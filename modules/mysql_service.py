from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import mysql.connector

from modules.cache_store import get_cached_value, invalidate_cached_values, set_cached_value
from modules.catalog import REST_ADMIN_ROLES, SPECIAL_PRIV_CATEGORIES, SPECIAL_ROLE_CATALOG, SYSTEM_USER_PREFIXES, SYSTEM_USERS
from modules.app_config import CONFIG, get_runtime_config
from modules.profile_session_store import get_profile_password


def is_system_user(username: str) -> bool:
    if username in SYSTEM_USERS:
        return True
    return any(username.startswith(prefix) for prefix in SYSTEM_USER_PREFIXES)


def stop_all_shared_tunnels() -> None:
    runtime_config = get_runtime_config()
    try:
        _manage_tunnel("stop", runtime_config)
    except Exception:
        return


def ensure_login_tunnels() -> None:
    from flask import has_request_context, session
    if has_request_context() and session.get("connection_profile", {}).get("mode") == "socket":
        return

    runtime_config = get_runtime_config()
    try:
        with socket.create_connection((runtime_config.host, runtime_config.port), timeout=runtime_config.connect_timeout):
            pass
    except OSError as exc:
        raise RuntimeError(
            f"DB tunnel is not reachable on {runtime_config.host}:{runtime_config.port}. Establish the local DB tunnel first."
        ) from exc

    try:
        with socket.create_connection((runtime_config.api_host, runtime_config.api_port), timeout=runtime_config.connect_timeout):
            pass
    except OSError as exc:
        raise RuntimeError(
            f"REST tunnel is not reachable on {runtime_config.api_host}:{runtime_config.api_port}. Establish the local REST tunnel first."
        ) from exc


def _connector_kwargs(username: str, password: str) -> dict[str, Any]:
    from flask import has_request_context, session

    if has_request_context():
        profile = session.get("connection_profile", {})
        if profile.get("mode") == "socket":
            socket_path = str(profile.get("socket", "")).strip()
            if not socket_path.startswith("/"):
                socket_path = str((Path(__file__).resolve().parent.parent / socket_path).resolve())
            return {
                "unix_socket": socket_path,
                "user": username,
                "password": password,
                "autocommit": True,
                "connection_timeout": get_runtime_config().connect_timeout,
            }

    runtime_config = get_runtime_config()
    return {
        "host": runtime_config.host,
        "port": runtime_config.port,
        "user": username,
        "password": password,
        "autocommit": True,
        "connection_timeout": runtime_config.connect_timeout,
    }


def mysqlsh_uri(username: str, password: str) -> str:
    from flask import has_request_context, session

    if has_request_context():
        profile = session.get("connection_profile", {})
        if profile.get("mode") == "socket":
            socket_path = str(profile.get("socket", "")).strip()
            if not socket_path.startswith("/"):
                socket_path = str((Path(__file__).resolve().parent.parent / socket_path).resolve())
            return f"mysql://{quote(username)}:{quote(password)}@localhost?socket={quote(socket_path, safe='')}"

    runtime_config = get_runtime_config()
    return f"mysql://{quote(username)}:{quote(password)}@{runtime_config.host}:{runtime_config.port}"


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote_char = ""
    escape = False
    for char in sql:
        current.append(char)
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote_char:
            if char == quote_char:
                quote_char = ""
            continue
        if char in {"'", '"', "`"}:
            quote_char = char
            continue
        if char == ";":
            statement = "".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _run_connector_sql(
    sql: str,
    *,
    username: str,
    password: str,
    raw_output: bool = False,
) -> list[dict[str, Any]] | str:
    connection = mysql.connector.connect(**_connector_kwargs(username, password))
    try:
        rows: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        for statement in _split_sql_statements(sql):
            cursor = connection.cursor(dictionary=True, buffered=True)
            try:
                cursor.execute(statement)
                if cursor.with_rows:
                    fetched = [dict(row) for row in cursor.fetchall()]
                    if raw_output:
                        if cursor.column_names:
                            raw_lines.append("\t".join(str(name) for name in cursor.column_names))
                        for row in fetched:
                            raw_lines.append("\t".join("" if value is None else str(value) for value in row.values()))
                    else:
                        rows.extend(fetched)
            finally:
                cursor.close()
        if raw_output:
            return "\n".join(raw_lines)
        return rows
    finally:
        connection.close()


_MYSQLSH_REST_PATTERNS = (
    re.compile(r"^SHOW\s+(?:CREATE\s+)?REST\b", re.IGNORECASE),
    re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?REST\b", re.IGNORECASE),
    re.compile(r"^ALTER\s+REST\b", re.IGNORECASE),
    re.compile(r"^DROP\s+REST\b", re.IGNORECASE),
)


def _strip_leading_sql_comments(statement: str) -> str:
    remaining = statement.lstrip()
    while remaining:
        if remaining.startswith("--") or remaining.startswith("#"):
            _, separator, tail = remaining.partition("\n")
            if not separator:
                return ""
            remaining = tail.lstrip()
            continue
        if remaining.startswith("/*"):
            end_index = remaining.find("*/")
            if end_index < 0:
                return ""
            remaining = remaining[end_index + 2 :].lstrip()
            continue
        return remaining
    return remaining


def _requires_mrs_sql_extensions(sql: str) -> bool:
    for statement in _split_sql_statements(sql):
        cleaned = _strip_leading_sql_comments(statement)
        if any(pattern.match(cleaned) for pattern in _MYSQLSH_REST_PATTERNS):
            return True
    return False


def _resolve_mysqlsh_path() -> str:
    configured_path = str(CONFIG.mysqlsh_path or "").strip()
    if configured_path and "/" in configured_path:
        if Path(configured_path).is_file():
            return configured_path
        raise RuntimeError(
            f"MySQL Shell was configured as {configured_path}, but that file does not exist. "
            "Install MySQL Shell or set MRS_WEBAPP_MYSQLSH to the correct absolute path."
        )

    resolved = shutil.which(configured_path or "mysqlsh")
    if resolved:
        return resolved

    for candidate in ("/usr/bin/mysqlsh", "/usr/local/bin/mysqlsh", "/opt/mysql/mysql-shell/bin/mysqlsh"):
        if Path(candidate).is_file():
            return candidate

    raise RuntimeError(
        "MySQL Shell executable 'mysqlsh' was not found. The REST Admin create/expose actions use MySQL Shell "
        "MRS SQL extensions, matching codexmrs/webapp/mysql_admin.py. Install MySQL Shell or set MRS_WEBAPP_MYSQLSH."
    )


def _run_mrs_sql_extensions(
    sql: str,
    *,
    username: str,
    password: str,
    raw_output: bool = False,
) -> list[dict[str, Any]] | str:
    cmd = [
        _resolve_mysqlsh_path(),
        "--sql",
        mysqlsh_uri(username, password),
        "--execute",
        sql,
    ]
    if not raw_output:
        cmd.insert(2, "--result-format=json")

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(get_runtime_config().connect_timeout * 4, 12),
        check=False,
    )

    combined_stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0:
        stderr_lines = [
            line.strip()
            for line in combined_stderr.splitlines()
            if line.strip()
            and not line.strip().startswith("WARNING:")
            and "Using a password on the command line interface can be insecure." not in line
        ]
        stdout_lines = [
            line.strip()
            for line in stdout.splitlines()
            if line.strip()
            and not line.strip().startswith("WARNING:")
            and "Using a password on the command line interface can be insecure." not in line
        ]
        detail = "\n".join(stderr_lines) or "\n".join(stdout_lines) or "mysqlsh execution failed"
        raise RuntimeError(detail)

    if raw_output:
        return stdout

    rows: list[dict[str, Any]] = []
    buffer: list[str] = []
    depth = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("WARNING:") or stripped.startswith("Using a password"):
            continue
        depth += stripped.count("{")
        depth -= stripped.count("}")
        buffer.append(stripped)
        if depth == 0 and buffer:
            rows.append(json.loads(" ".join(buffer)))
            buffer = []
    return rows


def _manage_tunnel(action: str, runtime_config) -> None:
    profile = {
        "use_ssh_tunnel": runtime_config.host == "127.0.0.1" and runtime_config.api_host == "127.0.0.1",
        "db_host": getattr(runtime_config, "db_host", None),
    }
    from flask import has_request_context, session

    if not has_request_context():
        return
    login_profile = session.get("connection_profile", {})
    if not login_profile.get("use_ssh_tunnel"):
        return

    script_path = Path(__file__).resolve().parent.parent / "manage_heatwave_tunnels.sh"
    socket_dir = Path(__file__).resolve().parent.parent / ".ssh-tunnels"
    cmd = [
        str(script_path),
        action,
        "--ssh-key",
        str(login_profile.get("ssh_key_path", runtime_config.ssh_key_path)),
        "--ssh-user",
        str(login_profile.get("ssh_jump_user", runtime_config.ssh_jump_user)),
        "--ssh-host",
        str(login_profile.get("ssh_jump_host", runtime_config.ssh_jump_host)),
        "--remote-db-host",
        str(login_profile.get("db_host", runtime_config.host)),
        "--remote-db-port",
        str(login_profile.get("db_port", 3306)),
        "--remote-api-port",
        str(login_profile.get("api_port", 443)),
        "--remote-api-host",
        str(login_profile.get("api_host", login_profile.get("db_host", runtime_config.api_host))),
        "--local-db-port",
        str(login_profile.get("local_db_port", runtime_config.port)),
        "--local-api-port",
        str(login_profile.get("local_api_port", runtime_config.api_port)),
        "--socket-dir",
        str(socket_dir),
        "--socket-name",
        str(login_profile.get("socket_name", "mysql-rest-console.sock")),
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(runtime_config.connect_timeout * 8, 20),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"Tunnel {action} failed").strip()
        raise RuntimeError(detail)


def start_shared_tunnels() -> None:
    runtime_config = get_runtime_config()
    _manage_tunnel("start", runtime_config)


def run_profile_sql(
    sql: str,
    *,
    username: str,
    password: str,
    raw_output: bool = False,
) -> list[dict[str, Any]] | str:
    if _requires_mrs_sql_extensions(sql):
        return _run_mrs_sql_extensions(sql, username=username, password=password, raw_output=raw_output)

    try:
        return _run_connector_sql(sql, username=username, password=password, raw_output=raw_output)
    except mysql.connector.Error as exc:
        raise RuntimeError(str(exc)) from exc


def run_admin_sql(sql: str, *, raw_output: bool = False) -> list[dict[str, Any]] | str:
    from flask import has_request_context, session

    if has_request_context() and session.get("connection_profile") and session.get("db_username"):
        password = get_profile_password(str(session.get("profile_credential_token", "")))
        if not password:
            raise RuntimeError("Profile DB session expired. Log in to the profile again.")
        return run_profile_sql(sql, username=str(session["db_username"]), password=password, raw_output=raw_output)
    return run_profile_sql(sql, username=CONFIG.admin_username, password=CONFIG.admin_password, raw_output=raw_output)


def run_admin_connector_sql(sql: str, *, raw_output: bool = True) -> list[dict[str, Any]] | str:
    from flask import has_request_context, session

    if has_request_context() and session.get("connection_profile") and session.get("db_username"):
        password = get_profile_password(str(session.get("profile_credential_token", "")))
        if not password:
            raise RuntimeError("Profile DB session expired. Log in to the profile again.")
        return _run_connector_sql(sql, username=str(session["db_username"]), password=password, raw_output=raw_output)
    return _run_connector_sql(sql, username=CONFIG.admin_username, password=CONFIG.admin_password, raw_output=raw_output)


def run_admin_ddl(sql: str) -> None:
    run_admin_sql(sql, raw_output=True)


def fetch_grants(username: str, password: str) -> list[str]:
    output = run_profile_sql("SHOW GRANTS", username=username, password=password, raw_output=True)
    rows = []
    for line in str(output).splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("WARNING:") or cleaned.startswith("Using a password"):
            continue
        if cleaned.startswith("Grants for "):
            continue
        rows.append(cleaned)
    return rows


def list_users_with_roles() -> list[dict[str, Any]]:
    cache_key = "users:roles"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rest_roles_sql = ", ".join(f"'{role}'" for role in REST_ADMIN_ROLES)
    rows = run_admin_sql(
        f"""
        SELECT
            u.User AS username,
            u.Host AS host,
            CASE
                WHEN u.User IN ('admin', 'administrator') OR EXISTS (
                    SELECT 1
                    FROM mysql.role_edges re
                    WHERE re.to_user = u.User
                      AND re.to_host = u.Host
                      AND re.from_user = 'administrator'
                ) THEN 'Admin'
                WHEN EXISTS (
                    SELECT 1
                    FROM mysql.role_edges re
                    WHERE re.to_user = u.User
                      AND re.to_host = u.Host
                      AND re.from_user IN ({rest_roles_sql})
                ) THEN 'Rest Admin'
                ELSE 'Rest User'
            END AS role_name
        FROM mysql.user u
        ORDER BY u.User, u.Host
        """
    )
    users: list[dict[str, Any]] = []
    for row in rows:
        username = str(row["username"])
        host = str(row["host"])
        users.append(
            {
                "username": username,
                "host": host,
                "role_name": str(row["role_name"]),
                "system": is_system_user(username),
                "key": f"{username}@{host}",
            }
        )
    return set_cached_value(cache_key, users)


def non_system_users() -> list[dict[str, Any]]:
    return [user for user in list_users_with_roles() if not user["system"]]


def list_databases() -> list[str]:
    cache_key = "dbs:list"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql("SHOW DATABASES")
    dbs = []
    for row in rows:
        values = list(row.values())
        if values:
            db_name = str(values[0])
            if db_name not in {"information_schema", "performance_schema"}:
                dbs.append(db_name)
    return set_cached_value(cache_key, dbs)


def list_tables(schema_name: str) -> list[str]:
    cache_key = f"tables:{schema_name}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT TABLE_NAME AS table_name
        FROM information_schema.tables
        WHERE table_schema = '{schema_name}'
        ORDER BY TABLE_NAME
        """
    )
    return set_cached_value(cache_key, ["*"] + [str(row["table_name"]) for row in rows])


def list_base_tables(schema_name: str) -> list[str]:
    cache_key = f"base-tables:{schema_name}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT TABLE_NAME AS table_name
        FROM information_schema.tables
        WHERE table_schema = '{schema_name}'
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """
    )
    return set_cached_value(cache_key, [str(row["table_name"]) for row in rows])


def list_table_columns(schema_name: str, table_name: str) -> list[dict[str, str]]:
    cache_key = f"columns:{schema_name}:{table_name}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT
            COLUMN_NAME AS column_name,
            COLUMN_KEY AS column_key
        FROM information_schema.columns
        WHERE table_schema = '{schema_name}'
          AND table_name = '{table_name}'
        ORDER BY ORDINAL_POSITION
        """
    )
    columns = [
        {
            "column_name": str(row["column_name"]),
            "column_key": str(row["column_key"]),
        }
        for row in rows
    ]
    return set_cached_value(cache_key, columns)


def list_schema_procedures(schema_name: str) -> list[str]:
    schema_name_clean = _require_identifier(schema_name, "Schema")
    cache_key = f"procedures:{schema_name_clean}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT ROUTINE_NAME AS routine_name
        FROM information_schema.routines
        WHERE routine_schema = '{schema_name_clean}'
          AND ROUTINE_TYPE = 'PROCEDURE'
        ORDER BY ROUTINE_NAME
        """
    )
    procedures = [str(row["routine_name"]) for row in rows]
    return set_cached_value(cache_key, procedures)


def list_procedure_parameters(schema_name: str, procedure_name: str) -> list[dict[str, str]]:
    schema_name_clean = _require_identifier(schema_name, "Schema")
    procedure_name_clean = _require_identifier(procedure_name, "Stored procedure name")
    cache_key = f"procedure-params:{schema_name_clean}:{procedure_name_clean}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT
            ORDINAL_POSITION AS ordinal_position,
            COALESCE(PARAMETER_MODE, 'IN') AS parameter_mode,
            PARAMETER_NAME AS parameter_name,
            DTD_IDENTIFIER AS data_type
        FROM information_schema.parameters
        WHERE specific_schema = '{schema_name_clean}'
          AND specific_name = '{procedure_name_clean}'
          AND PARAMETER_NAME IS NOT NULL
        ORDER BY ORDINAL_POSITION
        """
    )
    parameters = [
        {
            "mode": str(row["parameter_mode"]).upper(),
            "name": str(row["parameter_name"]),
            "type": str(row["data_type"]),
        }
        for row in rows
    ]
    return set_cached_value(cache_key, parameters)


def special_privilege_categories() -> list[dict[str, str]]:
    return SPECIAL_PRIV_CATEGORIES


def default_special_priv_category() -> str:
    return SPECIAL_PRIV_CATEGORIES[0]["slug"]


def list_mysql_admin_privileges() -> list[dict[str, str]]:
    cache_key = "privs:mysql-admin"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql("SHOW PRIVILEGES")
    privileges: list[dict[str, str]] = []
    for row in rows:
        lowered = {str(key).lower(): str(value) for key, value in row.items()}
        privilege_name = lowered.get("privilege", "")
        context = lowered.get("context", "")
        comment = lowered.get("comment", "")
        if not privilege_name or "server admin" not in context.lower():
            continue
        privileges.append(
            {
                "value": f"priv:{privilege_name}",
                "name": privilege_name,
                "context": context,
                "comment": comment,
            }
        )
    privileges.sort(key=lambda item: item["name"])
    return set_cached_value(cache_key, privileges)


def list_special_privileges(category: str) -> list[dict[str, str]]:
    if category in SPECIAL_ROLE_CATALOG:
        return SPECIAL_ROLE_CATALOG[category]
    if category == "mysql-admin":
        return list_mysql_admin_privileges()
    return []


def create_user_account(username: str, password: str, role_name: str, host: str = "%") -> None:
    statements = [
        f"CREATE USER IF NOT EXISTS `{username}`@`{host}` IDENTIFIED BY '{password}'",
        f"ALTER USER `{username}`@`{host}` IDENTIFIED BY '{password}'",
    ]
    if role_name == "Admin":
        statements.extend(
            [
                f"GRANT `administrator` TO `{username}`@`{host}`",
                f"SET DEFAULT ROLE ALL TO `{username}`@`{host}`",
            ]
        )
    elif role_name == "Rest Admin":
        statements.extend(
            [f"GRANT `{rest_role}` TO `{username}`@`{host}`" for rest_role in REST_ADMIN_ROLES]
            + [f"SET DEFAULT ROLE ALL TO `{username}`@`{host}`"]
        )
    run_admin_sql(";\n".join(statements))
    invalidate_cached_values("users:")


def delete_selected_users(selected_keys: list[str]) -> int:
    dropped = 0
    for key in selected_keys:
        username, _, host = key.partition("@")
        if not username or not host or is_system_user(username):
            continue
        run_admin_sql(f"DROP USER IF EXISTS `{username}`@`{host}`")
        dropped += 1
    if dropped:
        invalidate_cached_values("users:")
    return dropped


def grant_object_privileges(schema_name: str, table_name: str, privilege: str, selected_keys: list[str]) -> int:
    privilege_sql = "ALL PRIVILEGES" if privilege == "ALL" else privilege
    object_sql = f"`{schema_name}`.*" if table_name == "*" else f"`{schema_name}`.`{table_name}`"
    granted = 0
    for key in selected_keys:
        username, _, host = key.partition("@")
        if not username or not host or is_system_user(username):
            continue
        run_admin_sql(f"GRANT {privilege_sql} ON {object_sql} TO `{username}`@`{host}`")
        granted += 1
    if granted:
        invalidate_cached_values("users:")
    return granted


def grant_special_privileges(selected_items: list[str], selected_keys: list[str]) -> tuple[int, int]:
    role_names = [value.split(":", 1)[1] for value in selected_items if value.startswith("role:")]
    privilege_names = [value.split(":", 1)[1] for value in selected_items if value.startswith("priv:")]

    granted_users = 0
    for key in selected_keys:
        username, _, host = key.partition("@")
        if not username or not host or is_system_user(username):
            continue

        statements: list[str] = []
        for role_name in role_names:
            statements.append(f"GRANT `{role_name}` TO `{username}`@`{host}`")
        for privilege_name in privilege_names:
            statements.append(f"GRANT {privilege_name} ON *.* TO `{username}`@`{host}`")
        if role_names:
            statements.append(f"SET DEFAULT ROLE ALL TO `{username}`@`{host}`")
        if not statements:
            continue

        run_admin_sql(";\n".join(statements))
        granted_users += 1

    if granted_users:
        invalidate_cached_values("users:")
    return len(selected_items), granted_users


def classify_role(username: str, grants: list[str]) -> str:
    normalized_grants = [grant.upper() for grant in grants]

    if username in {CONFIG.admin_username, "administrator"}:
        return "admin"

    if any(rest_role.upper() in grant for rest_role in REST_ADMIN_ROLES for grant in normalized_grants):
        return "rest_admin"

    admin_markers = (
        "ALL PRIVILEGES ON *.*",
        "CREATE USER ON *.*",
        "GRANT OPTION",
        "SYSTEM_USER",
        "ROLE_ADMIN",
        "`ADMINISTRATOR`@",
    )
    if any(marker in grant for marker in admin_markers for grant in normalized_grants):
        return "admin"

    return "test_user"


def fallback_user_role(username: str, password: str) -> str | None:
    if CONFIG.admin_password and username == CONFIG.admin_username and password == CONFIG.admin_password:
        return "admin"
    if CONFIG.rest_admin_password and username == CONFIG.rest_admin_username and password == CONFIG.rest_admin_password:
        return "rest_admin"
    if CONFIG.test_password and username == CONFIG.test_username and password == CONFIG.test_password:
        return "test_user"
    return None


def _quote_identifier(name: str) -> str:
    return f"`{name}`"


def _require_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9_]+", cleaned):
        raise RuntimeError(f"{label} must use letters, numbers, or underscores only.")
    return cleaned


def _slugify_path_segment(value: str, label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip().lower())
    cleaned = cleaned.strip("-_")
    if not cleaned:
        raise RuntimeError(f"{label} is required.")
    return cleaned


def _safe_identifier(*parts: str, max_length: int = 64) -> str:
    tokens = [re.sub(r"[^A-Za-z0-9_]+", "_", part.strip().lower()).strip("_") for part in parts]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise RuntimeError("Identifier generation failed.")
    identifier = "_".join(tokens)
    if len(identifier) <= max_length:
        return identifier
    trimmed = identifier[:max_length].rstrip("_")
    return trimmed or identifier[:max_length]


def _comment_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ensure_rest_auth_app(service_path: str) -> list[str]:
    auth_app = "MySQL"
    return [
        f'CREATE REST AUTHENTICATION APP IF NOT EXISTS "{auth_app}" VENDOR MYSQL',
        f'ALTER REST SERVICE {service_path} ADD AUTH APP "{auth_app}"',
    ]


def build_rest_service_path_definition(*, service_name: str) -> dict[str, str]:
    service_slug = _slugify_path_segment(service_name, "REST service path name")
    service_path = f"/{service_slug}"
    sql = (
        f"CREATE OR REPLACE REST SERVICE {service_path}\n"
        f"    COMMENT {_comment_literal(f'Rest service path {service_path}')}\n"
        f"    PUBLISHED"
    )
    return {
        "sql": sql,
        "service_path": service_path,
    }


def create_rest_service_path_definition(*, service_name: str) -> dict[str, str]:
    result = build_rest_service_path_definition(service_name=service_name)
    run_admin_ddl(result["sql"])
    invalidate_cached_values("rest-services:")
    return result


def build_expose_database_to_service_definition(
    *,
    service_path: str,
    source_schema: str,
    auth_required: bool,
) -> dict[str, str]:
    normalized_service_path = service_path.strip()
    if not normalized_service_path.startswith("/"):
        raise RuntimeError("REST service path must start with '/'.")

    source_schema_name = _require_identifier(source_schema, "Database")
    schema_path = f"/{source_schema_name.lower()}"
    auth_mode = "REQUIRED" if auth_required else "NOT REQUIRED"
    statements: list[str] = []
    if auth_required:
        statements.extend(_ensure_rest_auth_app(normalized_service_path))
    statements.append(
        f"CREATE OR REPLACE REST SCHEMA {schema_path} ON SERVICE {normalized_service_path}\n"
        f"    FROM {_quote_identifier(source_schema_name)}\n"
        f"    ENABLED\n"
        f"    AUTHENTICATION {auth_mode}\n"
        f"    COMMENT {_comment_literal(f'Rest schema for {source_schema_name}')}"
    )
    sql = ";\n\n".join(statements)
    return {
        "sql": sql,
        "service_path": normalized_service_path,
        "schema_path": schema_path,
        "source_schema": source_schema_name,
        "auth_required": "Required" if auth_required else "Not Required",
    }


def expose_database_to_service_definition(
    *,
    service_path: str,
    source_schema: str,
    auth_required: bool,
) -> dict[str, str]:
    result = build_expose_database_to_service_definition(
        service_path=service_path,
        source_schema=source_schema,
        auth_required=auth_required,
    )
    run_admin_ddl(result["sql"])
    invalidate_cached_values("rest-services:")
    return result


def build_rest_service_definition(
    *,
    service_path: str,
    source_schema: str,
    source_table: str,
    auth_required: bool,
) -> dict[str, str]:
    normalized_service_path = service_path.strip()
    if not normalized_service_path.startswith("/"):
        raise RuntimeError("REST service path must start with '/'.")
    source_schema_name = _require_identifier(source_schema, "Database")
    source_table_name = _require_identifier(source_table, "Table")

    columns = list_table_columns(source_schema_name, source_table_name)
    if not columns:
        raise RuntimeError("The selected table has no columns to expose.")

    schema_name = "restapidb"
    schema_path = f"/{schema_name}"
    object_path = f"/{source_table_name.lower()}"
    view_name = _safe_identifier("vw", normalized_service_path, source_schema_name, source_table_name)
    auth_mode = "REQUIRED" if auth_required else "NOT REQUIRED"

    field_lines: list[str] = []
    for column in columns:
        column_name = column["column_name"]
        sortable = " @SORTABLE" if column["column_key"] == "PRI" else ""
        field_lines.append(f"    {column_name}: {column_name}{sortable}")
    field_mapping = ",\n".join(field_lines)

    statements = [
        f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(schema_name)}",
        (
            f"CREATE OR REPLACE SQL SECURITY DEFINER VIEW {_quote_identifier(schema_name)}.{_quote_identifier(view_name)} AS\n"
            f"SELECT *\nFROM {_quote_identifier(source_schema_name)}.{_quote_identifier(source_table_name)}"
        ),
    ]

    if auth_required:
        statements.extend(_ensure_rest_auth_app(normalized_service_path))

    statements.extend(
        [
            (
                f"CREATE OR REPLACE REST SCHEMA {schema_path} ON SERVICE {normalized_service_path}\n"
                f"    FROM {_quote_identifier(schema_name)}\n"
                f"    ENABLED\n"
                f"    AUTHENTICATION {auth_mode}\n"
                f"    COMMENT {_comment_literal(f'Rest schema for {source_schema_name}.{source_table_name}')}"
            ),
            (
                f"CREATE OR REPLACE REST VIEW {object_path}\n"
                f"ON SERVICE {normalized_service_path} SCHEMA {schema_path}\n"
                f"AS {_quote_identifier(schema_name)}.{_quote_identifier(view_name)} {{\n"
                f"{field_mapping}\n"
                f"}}\n"
                f"AUTHENTICATION {auth_mode}\n"
                f"COMMENT {_comment_literal(f'Endpoint for {source_schema_name}.{source_table_name}')}"
            ),
        ]
    )

    sql = ";\n\n".join(statements)
    return {
        "sql": sql,
        "service_path": normalized_service_path,
        "schema_path": schema_path,
        "object_path": object_path,
        "endpoint": f"{normalized_service_path}{schema_path}{object_path}",
        "view_name": view_name,
        "auth_required": "Required" if auth_required else "Not Required",
        "source_table": f"{source_schema_name}.{source_table_name}",
    }


def create_rest_service_definition(
    *,
    service_path: str,
    source_schema: str,
    source_table: str,
    auth_required: bool,
) -> dict[str, str]:
    result = build_rest_service_definition(
        service_path=service_path,
        source_schema=source_schema,
        source_table=source_table,
        auth_required=auth_required,
    )
    run_admin_ddl(result["sql"])
    invalidate_cached_values("rest-objects:", "rest-services:", "base-tables:", "columns:")
    return result


def list_rest_service_paths() -> list[str]:
    cache_key = "rest-services:paths"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        """
        SELECT url_context_root AS service_path
        FROM mysql_rest_service_metadata.service
        WHERE published = 1
        ORDER BY url_context_root
        """
    )
    service_paths = [
        str(row.get("service_path", "")).strip()
        for row in rows
        if str(row.get("service_path", "")).strip().startswith("/")
    ]
    return set_cached_value(cache_key, service_paths)


def build_rest_procedure_definition(
    *,
    procedure_name: str,
    service_path: str,
    auth_required: bool,
    parameters: list[dict[str, str]],
    body_sql: str,
) -> dict[str, str]:
    procedure_name_clean = _require_identifier(procedure_name, "Stored procedure name")
    normalized_service_path = service_path.strip()
    if not normalized_service_path.startswith("/"):
        raise RuntimeError("REST service path must start with '/'.")

    normalized_body = body_sql.strip().rstrip(";").strip()
    if not normalized_body:
        raise RuntimeError("Stored procedure body is required.")
    if ";" in normalized_body:
        raise RuntimeError("Stored procedure body currently supports a single SQL statement only.")

    parameter_defs: list[str] = []
    rest_parameter_defs: list[str] = []
    for parameter in parameters:
        param_name = _require_identifier(parameter["name"], "Parameter name")
        param_type = parameter["type"].strip()
        if not param_type:
            raise RuntimeError("Parameter type is required.")
        mode = parameter["mode"].strip().upper()
        if mode not in {"IN", "OUT", "INOUT"}:
            raise RuntimeError("Parameter mode must be IN, OUT, or INOUT.")
        parameter_defs.append(f"{mode} {_quote_identifier(param_name)} {param_type}")
        rest_parameter_defs.append(f"    {param_name}: {param_name} @{mode}")

    procedure_signature = f"({', '.join(parameter_defs)})" if parameter_defs else "()"
    sql_parts = [
        f"DROP PROCEDURE IF EXISTS {_quote_identifier('restapidb')}.{_quote_identifier(procedure_name_clean)}",
        (
            f"CREATE PROCEDURE {_quote_identifier('restapidb')}.{_quote_identifier(procedure_name_clean)}{procedure_signature}\n"
            f"SQL SECURITY DEFINER\n"
            f"READS SQL DATA\n"
            f"{normalized_body}"
        ),
    ]

    rest_procedure_sql = (
        f"CREATE OR REPLACE REST PROCEDURE /{procedure_name_clean.lower()}\n"
        f"ON SERVICE {normalized_service_path} SCHEMA /restapidb\n"
        f"AS restapidb.{procedure_name_clean}\n"
    )
    if rest_parameter_defs:
        rest_procedure_sql += "PARAMETERS RestApiDbProcedureParams {\n" + ",\n".join(rest_parameter_defs) + "\n}\n"
    rest_procedure_sql += (
        f"AUTHENTICATION {'REQUIRED' if auth_required else 'NOT REQUIRED'}\n"
        f"COMMENT {_comment_literal(f'Rest procedure for restapidb.{procedure_name_clean}')}"
    )
    sql_parts.append(rest_procedure_sql)

    sql = ";\n\n".join(sql_parts)
    return {
        "sql": sql,
        "procedure_name": procedure_name_clean,
        "service_path": normalized_service_path,
        "endpoint": f"{normalized_service_path}/restapidb/{procedure_name_clean.lower()}",
        "auth_required": "Required" if auth_required else "Not Required",
    }


def create_rest_procedure_definition(
    *,
    procedure_name: str,
    service_path: str,
    auth_required: bool,
    parameters: list[dict[str, str]],
    body_sql: str,
) -> dict[str, str]:
    result = build_rest_procedure_definition(
        procedure_name=procedure_name,
        service_path=service_path,
        auth_required=auth_required,
        parameters=parameters,
        body_sql=body_sql,
    )
    run_admin_ddl(result["sql"])
    invalidate_cached_values("rest-objects:", "rest-services:")
    return result


def build_expose_existing_schema_procedure(
    *,
    source_schema: str,
    procedure_name: str,
    service_path: str,
    auth_required: bool,
) -> dict[str, str]:
    source_schema_name = _require_identifier(source_schema, "Schema")
    procedure_name_clean = _require_identifier(procedure_name, "Stored procedure name")
    normalized_service_path = service_path.strip()
    if not normalized_service_path.startswith("/"):
        raise RuntimeError("REST service path must start with '/'.")

    parameters = list_procedure_parameters(source_schema_name, procedure_name_clean)
    schema_path = "/restapidb"
    auth_mode = "REQUIRED" if auth_required else "NOT REQUIRED"
    wrapper_name = _safe_identifier(source_schema_name, procedure_name_clean, "wrapper")
    sql_parts: list[str] = []

    parameter_defs: list[str] = []
    rest_parameter_defs: list[str] = []
    call_arguments: list[str] = []
    for parameter in parameters:
        param_name = _require_identifier(parameter["name"], "Parameter name")
        param_type = parameter["type"].strip()
        if not param_type:
            raise RuntimeError(f"Parameter type metadata is missing for {source_schema_name}.{procedure_name_clean}.")
        mode = parameter["mode"].strip().upper()
        if mode not in {"IN", "OUT", "INOUT"}:
            mode = "IN"
        parameter_defs.append(f"{mode} {_quote_identifier(param_name)} {param_type}")
        rest_parameter_defs.append(f"    {param_name}: {param_name} @{mode}")
        call_arguments.append(param_name)

    wrapper_signature = f"({', '.join(parameter_defs)})" if parameter_defs else "()"
    wrapper_call = f"CALL {source_schema_name}.{procedure_name_clean}({', '.join(call_arguments)})"

    if auth_required:
        sql_parts.extend(_ensure_rest_auth_app(normalized_service_path))

    sql_parts.extend(
        [
            f"CREATE DATABASE IF NOT EXISTS {_quote_identifier('restapidb')}",
            (
                f"CREATE OR REPLACE REST SCHEMA {schema_path} ON SERVICE {normalized_service_path}\n"
                f"    FROM {_quote_identifier('restapidb')}\n"
                f"    ENABLED\n"
                f"    AUTHENTICATION {auth_mode}\n"
                f"    COMMENT {_comment_literal(f'Rest schema for exposing {source_schema_name} procedures through restapidb')}"
            ),
            f"DROP PROCEDURE IF EXISTS {_quote_identifier('restapidb')}.{_quote_identifier(wrapper_name)}",
            (
                f"CREATE PROCEDURE {_quote_identifier('restapidb')}.{_quote_identifier(wrapper_name)}{wrapper_signature}\n"
                f"SQL SECURITY DEFINER\n"
                f"{wrapper_call}"
            ),
            (
                f"CREATE OR REPLACE REST PROCEDURE /{procedure_name_clean.lower()}\n"
                f"ON SERVICE {normalized_service_path} SCHEMA {schema_path}\n"
                f"AS restapidb.{wrapper_name}\n"
            f"COMMENT {_comment_literal(f'Rest procedure for {source_schema_name}.{procedure_name_clean}')}"
            ),
        ]
    )

    if rest_parameter_defs:
        rest_parameter_block = "PARAMETERS RestProcedureParams {\n" + ",\n".join(rest_parameter_defs) + "\n}\n"
        sql_parts[-1] = (
            f"CREATE OR REPLACE REST PROCEDURE /{procedure_name_clean.lower()}\n"
            f"ON SERVICE {normalized_service_path} SCHEMA {schema_path}\n"
            f"AS restapidb.{wrapper_name}\n"
            f"{rest_parameter_block}"
            f"AUTHENTICATION {auth_mode}\n"
            f"COMMENT {_comment_literal(f'Rest procedure for {source_schema_name}.{procedure_name_clean}')}"
        )
    else:
        sql_parts[-1] = (
            f"CREATE OR REPLACE REST PROCEDURE /{procedure_name_clean.lower()}\n"
            f"ON SERVICE {normalized_service_path} SCHEMA {schema_path}\n"
            f"AS restapidb.{wrapper_name}\n"
            f"AUTHENTICATION {auth_mode}\n"
            f"COMMENT {_comment_literal(f'Rest procedure for {source_schema_name}.{procedure_name_clean}')}"
        )

    sql = ";\n\n".join(sql_parts)
    return {
        "sql": sql,
        "procedure_name": procedure_name_clean,
        "service_path": normalized_service_path,
        "endpoint": f"{normalized_service_path}{schema_path}/{procedure_name_clean.lower()}",
        "auth_required": "Required" if auth_required else "Not Required",
        "schema_path": schema_path,
        "wrapper_name": wrapper_name,
    }


def expose_existing_schema_procedure(
    *,
    source_schema: str,
    procedure_name: str,
    service_path: str,
    auth_required: bool,
) -> dict[str, str]:
    result = build_expose_existing_schema_procedure(
        source_schema=source_schema,
        procedure_name=procedure_name,
        service_path=service_path,
        auth_required=auth_required,
    )
    run_admin_ddl(result["sql"])
    invalidate_cached_values("rest-objects:", "rest-services:", "procedure-params:")
    return result
