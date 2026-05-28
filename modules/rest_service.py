from __future__ import annotations

import json
import re
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from modules.cache_store import get_cached_value, set_cached_value
from modules.app_config import get_runtime_config
from modules.mysql_service import run_admin_sql


def list_restapidb_objects(schema_name: str = "restapidb") -> dict[str, list[str]]:
    cache_key = f"rest-objects:{schema_name}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    table_rows = run_admin_sql(
        f"""
        SELECT TABLE_NAME AS object_name
        FROM information_schema.tables
        WHERE table_schema = '{schema_name}'
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """
    )
    view_rows = run_admin_sql(
        f"""
        SELECT TABLE_NAME AS object_name
        FROM information_schema.tables
        WHERE table_schema = '{schema_name}'
          AND TABLE_TYPE = 'VIEW'
        ORDER BY TABLE_NAME
        """
    )
    procedure_rows = run_admin_sql(
        f"""
        SELECT ROUTINE_NAME AS object_name
        FROM information_schema.routines
        WHERE routine_schema = '{schema_name}'
          AND ROUTINE_TYPE = 'PROCEDURE'
        ORDER BY ROUTINE_NAME
        """
    )
    return set_cached_value(
        cache_key,
        {
            "tables": [str(row["object_name"]) for row in table_rows],
            "views": [str(row["object_name"]) for row in view_rows],
            "procedures": [str(row["object_name"]) for row in procedure_rows],
        },
    )


def parse_tabbed_output(raw: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("WARNING:") or cleaned.startswith("Using a password"):
            continue
        rows.append([part.strip() for part in cleaned.split("\t")])
    return rows


def extract_rest_auth_mode(raw: str) -> str:
    return "Required" if "AUTHENTICATION REQUIRED" in raw.upper() else "Not Required"


def extract_rest_auth_path(raw: str) -> str:
    match = re.search(r'AUTHENTICATION\s+PATH\s+"([^"]+)"', raw, flags=re.IGNORECASE)
    if match:
        auth_path = match.group(1).strip()
        if not auth_path.startswith("/"):
            auth_path = f"/{auth_path}"
        return auth_path
    return "/authentication"


def extract_endpoint_parameters(*texts: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    patterns = (
        re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}"),
        re.compile(r":([A-Za-z_][A-Za-z0-9_]*)"),
    )
    for text in texts:
        for pattern in patterns:
            for match in pattern.findall(text):
                if match not in seen:
                    seen.add(match)
                    ordered.append(match)
    return ordered


def parse_rest_procedure_parameters(raw: str) -> list[dict[str, str]]:
    normalized_raw = raw.replace("\\n", "\n")
    match = re.search(
        r"PARAMETERS\s+\w+\s*\{(?P<body>.*?)\}\s*AUTHENTICATION",
        normalized_raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []

    parameters: list[dict[str, str]] = []
    for line in match.group("body").splitlines():
        cleaned = line.strip().rstrip(",")
        if not cleaned or ":" not in cleaned:
            continue
        rest_name, remainder = cleaned.split(":", 1)
        rest_name = rest_name.strip()
        source_name_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", remainder)
        mode_match = re.search(r"@([A-Za-z]+)", remainder)
        if not rest_name or source_name_match is None:
            continue
        parameters.append(
            {
                "name": rest_name,
                "source_name": source_name_match.group(1),
                "mode": (mode_match.group(1).upper() if mode_match else "IN"),
            }
        )
    return parameters


def normalize_auth_app(raw_auth_apps: str) -> str:
    if not raw_auth_apps or raw_auth_apps in {"NULL", "-"}:
        return ""
    cleaned = raw_auth_apps.strip()
    if cleaned in {"", "NULL", "-"}:
        return ""
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and parsed:
                return str(parsed[0]).strip()
        except json.JSONDecodeError:
            pass
    first = cleaned.split(",")[0].strip()
    return first.strip("[]\"'` ")


def infer_auth_required(raw_auth_apps: str) -> str:
    return "Required" if normalize_auth_app(raw_auth_apps) else "Not Required"


def _parse_rest_target(raw: str) -> tuple[str, str] | None:
    match = re.search(r"AS\s+`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?", raw, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1), match.group(2)


def _lookup_table_type(schema_name: str, object_name: str) -> str:
    rows = run_admin_sql(
        f"""
        SELECT TABLE_TYPE AS table_type
        FROM information_schema.tables
        WHERE table_schema = '{schema_name}'
          AND table_name = '{object_name}'
        LIMIT 1
        """
    )
    if not rows:
        return ""
    return str(rows[0].get("table_type", "")).upper()


def _resolve_view_source(schema_name: str, view_name: str) -> tuple[str, str]:
    table_type = _lookup_table_type(schema_name, view_name)
    if table_type == "BASE TABLE":
        return schema_name, view_name

    if table_type == "VIEW":
        create_output = str(run_admin_sql(f"SHOW CREATE VIEW `{schema_name}`.`{view_name}`;", raw_output=True))
        matches = re.findall(r"from\s+`([A-Za-z0-9_]+)`\.`([A-Za-z0-9_]+)`", create_output, flags=re.IGNORECASE)
        if matches:
            source_schema, source_object = matches[0]
            return source_schema, source_object
    return schema_name, view_name


def _resolve_procedure_source(schema_name: str, procedure_name: str) -> tuple[str, str]:
    create_output = str(run_admin_sql(f"SHOW CREATE PROCEDURE `{schema_name}`.`{procedure_name}`;", raw_output=True))
    match = re.search(r"CALL\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*\(", create_output, flags=re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return schema_name, procedure_name


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_view_source_map(schema_name: str, view_names: list[str]) -> dict[str, tuple[str, str]]:
    if not view_names:
        return {}

    in_list = ", ".join(_quote_sql_string(name) for name in sorted(set(view_names)))
    rows = run_admin_sql(
        f"""
        SELECT TABLE_NAME AS object_name, VIEW_DEFINITION AS definition
        FROM information_schema.views
        WHERE table_schema = '{schema_name}'
          AND TABLE_NAME IN ({in_list})
        """
    )
    mapping: dict[str, tuple[str, str]] = {}
    for row in rows:
        object_name = str(row.get("object_name", ""))
        definition = str(row.get("definition", ""))
        match = re.search(r"from\s+`([A-Za-z0-9_]+)`\.`([A-Za-z0-9_]+)`", definition, flags=re.IGNORECASE)
        if match:
            mapping[object_name] = (match.group(1), match.group(2))
        else:
            mapping[object_name] = (schema_name, object_name)
    return mapping


def _load_routine_source_map(schema_name: str, routine_names: list[str]) -> dict[str, tuple[str, str]]:
    if not routine_names:
        return {}

    in_list = ", ".join(_quote_sql_string(name) for name in sorted(set(routine_names)))
    rows = run_admin_sql(
        f"""
        SELECT ROUTINE_NAME AS object_name, ROUTINE_DEFINITION AS definition
        FROM information_schema.routines
        WHERE routine_schema = '{schema_name}'
          AND ROUTINE_NAME IN ({in_list})
        """
    )
    mapping: dict[str, tuple[str, str]] = {}
    for row in rows:
        object_name = str(row.get("object_name", ""))
        definition = str(row.get("definition", ""))
        match = re.search(r"CALL\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*\(", definition, flags=re.IGNORECASE)
        if match:
            mapping[object_name] = (match.group(1), match.group(2))
        else:
            mapping[object_name] = (schema_name, object_name)
    return mapping


def resolve_rest_source_details(
    *,
    service_path: str,
    schema_path: str,
    object_path: str,
    object_kind: str,
) -> dict[str, str]:
    cache_key = f"rest-source:{service_path}:{schema_path}:{object_kind}:{object_path}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT
            ds.name AS target_schema,
            dbo.name AS target_object
        FROM mysql_rest_service_metadata.service s
        JOIN mysql_rest_service_metadata.db_schema ds ON ds.service_id = s.id
        JOIN mysql_rest_service_metadata.db_object dbo ON dbo.db_schema_id = ds.id
        WHERE s.url_context_root = {_quote_sql_string(service_path)}
          AND ds.request_path = {_quote_sql_string(schema_path)}
          AND dbo.request_path = {_quote_sql_string(object_path)}
        LIMIT 1
        """
    )
    if not rows:
        source_schema = schema_path.lstrip("/") or "-"
        source_object = object_path.lstrip("/") or "-"
        return set_cached_value(
            cache_key,
            {
                "rest_schema": schema_path.lstrip("/") or "-",
                "source_schema": source_schema,
                "source_object": source_object,
            },
        )

    target_schema = str(rows[0].get("target_schema", "")).strip() or schema_path.lstrip("/") or "-"
    target_object = str(rows[0].get("target_object", "")).strip() or object_path.lstrip("/") or "-"
    source_schema = target_schema
    source_object = target_object

    try:
        if object_kind == "VIEW":
            source_schema, source_object = _resolve_view_source(target_schema, target_object)
        elif object_kind == "PROCEDURE":
            source_schema, source_object = _resolve_procedure_source(target_schema, target_object)
    except Exception:
        source_schema, source_object = target_schema, target_object

    return set_cached_value(
        cache_key,
        {
            "rest_schema": schema_path.lstrip("/") or "-",
            "source_schema": source_schema,
            "source_object": source_object,
        },
    )


def get_rest_procedure_details(
    *,
    service_path: str,
    schema_path: str,
    object_path: str,
) -> dict[str, Any]:
    cache_key = f"rest-procedure:{service_path}:{schema_path}:{object_path}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        f"""
        SELECT
            ds.name AS routine_schema,
            dbo.name AS routine_name
        FROM mysql_rest_service_metadata.service s
        JOIN mysql_rest_service_metadata.db_schema ds ON ds.service_id = s.id
        JOIN mysql_rest_service_metadata.db_object dbo ON dbo.db_schema_id = ds.id
        WHERE s.url_context_root = {_quote_sql_string(service_path)}
          AND ds.request_path = {_quote_sql_string(schema_path)}
          AND dbo.request_path = {_quote_sql_string(object_path)}
          AND dbo.object_type IN ('PROCEDURE', 'FUNCTION')
        LIMIT 1
        """
    )
    procedure_params: list[dict[str, str]] = []
    if rows:
        routine_schema = str(rows[0].get("routine_schema", "")).strip()
        routine_name = str(rows[0].get("routine_name", "")).strip()
        if routine_schema and routine_name:
            parameter_rows = run_admin_sql(
                f"""
                SELECT
                    COALESCE(PARAMETER_MODE, 'IN') AS parameter_mode,
                    PARAMETER_NAME AS parameter_name
                FROM information_schema.parameters
                WHERE specific_schema = {_quote_sql_string(routine_schema)}
                  AND specific_name = {_quote_sql_string(routine_name)}
                  AND PARAMETER_NAME IS NOT NULL
                ORDER BY ORDINAL_POSITION
                """
            )
            procedure_params = [
                {
                    "name": str(row.get("parameter_name", "")),
                    "source_name": str(row.get("parameter_name", "")),
                    "mode": str(row.get("parameter_mode", "IN")).upper(),
                }
                for row in parameter_rows
                if str(row.get("parameter_name", "")).strip()
            ]

    return set_cached_value(
        cache_key,
        {
            "procedure_params": procedure_params,
        },
    )


def parse_json_if_possible(body: str) -> Any | None:
    stripped = body.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def normalize_rest_response(parsed_body: Any, *, object_kind: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "out_parameters": {},
        "result_sets": [],
        "json_body": parsed_body,
    }
    if parsed_body is None:
        return normalized

    if object_kind != "PROCEDURE":
        if isinstance(parsed_body, list):
            normalized["result_sets"] = [{"name": "Rows", "rows": parsed_body}]
        elif isinstance(parsed_body, dict):
            normalized["result_sets"] = [{"name": "Response", "rows": [parsed_body]}]
        return normalized

    if isinstance(parsed_body, list):
        normalized["result_sets"] = [{"name": "Result Set 1", "rows": parsed_body}]
        return normalized

    if not isinstance(parsed_body, dict):
        return normalized

    if "_metadata" in parsed_body and "items" in parsed_body:
        normalized["result_sets"] = [
            {
                "name": "Result Set 1",
                "_metadata": parsed_body.get("_metadata"),
                "items": parsed_body.get("items", []),
            }
        ]
        return normalized

    out_parameters = {}
    for key in ("outParameters", "out_parameters", "outParams", "out_params"):
        value = parsed_body.get(key)
        if isinstance(value, dict):
            out_parameters = value
            break
    normalized["out_parameters"] = out_parameters

    result_sets: list[dict[str, Any]] = []
    for key in ("resultSets", "result_sets", "resultsets"):
        value = parsed_body.get(key)
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                if isinstance(item, dict) and "_metadata" in item and "items" in item:
                    result_sets.append(
                        {
                            "name": str(item.get("name") or f"Result Set {index}"),
                            "_metadata": item.get("_metadata"),
                            "items": item.get("items", []),
                        }
                    )
                elif isinstance(item, dict) and "rows" in item and isinstance(item.get("rows"), list):
                    result_sets.append(
                        {
                            "name": str(item.get("name") or f"Result Set {index}"),
                            "rows": item.get("rows", []),
                        }
                    )
                else:
                    result_sets.append(
                        {
                            "name": f"Result Set {index}",
                            "rows": item if isinstance(item, list) else [item],
                        }
                    )
            break

    if not result_sets:
        candidate_sets = []
        for key, value in parsed_body.items():
            if key in {"outParameters", "out_parameters", "outParams", "out_params"}:
                continue
            if isinstance(value, list):
                candidate_sets.append(
                    {
                        "name": key,
                        "rows": value,
                    }
                )
        if candidate_sets:
            result_sets = candidate_sets

    normalized["result_sets"] = result_sets
    return normalized


def build_result_set_tables(result_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return []

    rendered_tables: list[dict[str, Any]] = []
    for index, result_set in enumerate(result_sets, start=1):
        metadata = result_set.get("_metadata")
        rows = result_set.get("items", result_set.get("rows", []))
        if not isinstance(rows, list):
            rows = [rows]
        try:
            columns: list[str] = []
            if isinstance(metadata, dict):
                metadata = metadata.get("fields", metadata)
            if isinstance(metadata, list):
                for item in metadata:
                    if isinstance(item, dict):
                        column_name = (
                            item.get("name")
                            or item.get("fieldName")
                            or item.get("columnName")
                            or item.get("label")
                        )
                        columns.append(str(column_name or ""))
                    else:
                        columns.append(str(item))

            if columns and all(not isinstance(row, dict) for row in rows):
                normalized_rows = []
                for row in rows:
                    if isinstance(row, (list, tuple)):
                        normalized_rows.append(list(row))
                    else:
                        normalized_rows.append([row])
                frame = pd.DataFrame(normalized_rows, columns=columns)
            else:
                frame = pd.DataFrame(rows)
                if columns and len(columns) == len(frame.columns):
                    frame.columns = columns
            html = frame.to_html(
                index=False,
                classes=["data-table", "pandas-table"],
                border=0,
                na_rep="",
                escape=True,
            )
        except Exception:
            continue
        rendered_tables.append(
            {
                "name": str(result_set.get("name") or f"Result Set {index}"),
                "html": html,
                "row_count": len(rows),
            }
        )
    return rendered_tables


def get_rest_service_auth_details(
    service_path: str,
    *,
    schema_path: str = "/restapidb",
    auth_apps: str = "",
) -> dict[str, str]:
    cache_key = f"rest-auth:{service_path}:{schema_path}"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    auth_required = infer_auth_required(auth_apps)
    auth_path = "/authentication"
    rows = run_admin_sql(
        f"""
        SELECT
            s.auth_path AS auth_path,
            CASE
                WHEN COALESCE(ds.requires_auth, 0) = 1 THEN 'Required'
                ELSE 'Not Required'
            END AS auth_required
        FROM mysql_rest_service_metadata.service s
        LEFT JOIN mysql_rest_service_metadata.db_schema ds ON ds.service_id = s.id
            AND ds.request_path = {_quote_sql_string(schema_path)}
        WHERE s.url_context_root = {_quote_sql_string(service_path)}
        LIMIT 1
        """
    )
    if rows:
        auth_required = str(rows[0].get("auth_required", auth_required)) or auth_required
        auth_path = str(rows[0].get("auth_path", auth_path)) or auth_path

    return set_cached_value(
        cache_key,
        {
            "auth_required": auth_required,
            "auth_path": auth_path,
            "auth_app_name": normalize_auth_app(auth_apps),
        },
    )


def list_restapidb_services(schema_name: str = "restapidb") -> list[dict[str, str]]:
    cache_key = "rest-services:all"
    cached = get_cached_value(cache_key)
    if cached is not None:
        return cached

    rows = run_admin_sql(
        """
        SELECT
            s.url_context_root AS service_path,
            COALESCE(GROUP_CONCAT(DISTINCT aa.name ORDER BY aa.name SEPARATOR ','), '') AS auth_apps,
            CASE WHEN s.enabled = 1 THEN 'ENABLED' ELSE 'DISABLED' END AS service_enabled,
            s.auth_path AS auth_path,
            ds.name AS db_schema_name,
            ds.request_path AS schema_path,
            CASE
                WHEN ds.id IS NULL THEN '-'
                WHEN ds.enabled = 1 THEN 'ENABLED'
                ELSE 'DISABLED'
            END AS schema_enabled,
            COALESCE(dbo.name, '') AS db_object_name,
            COALESCE(dbo.request_path, '') AS object_path,
            COALESCE(dbo.object_type, 'SCHEMA') AS object_kind,
            CASE
                WHEN dbo.id IS NULL THEN '-'
                WHEN dbo.enabled = 1 THEN 'ENABLED'
                ELSE 'DISABLED'
            END AS object_enabled,
            CASE
                WHEN COALESCE(dbo.requires_auth, ds.requires_auth, 0) = 1 THEN 'Required'
                ELSE 'Not Required'
            END AS auth_required
        FROM mysql_rest_service_metadata.service s
        LEFT JOIN mysql_rest_service_metadata.service_has_auth_app sha ON sha.service_id = s.id
        LEFT JOIN mysql_rest_service_metadata.auth_app aa ON aa.id = sha.auth_app_id
        LEFT JOIN mysql_rest_service_metadata.db_schema ds ON ds.service_id = s.id
        LEFT JOIN mysql_rest_service_metadata.db_object dbo ON dbo.db_schema_id = ds.id
        WHERE s.published = 1
        GROUP BY
            s.id,
            s.url_context_root,
            s.enabled,
            s.auth_path,
            ds.id,
            ds.name,
            ds.request_path,
            ds.enabled,
            dbo.id,
            dbo.name,
            dbo.request_path,
            dbo.object_type,
            dbo.enabled,
            dbo.requires_auth,
            ds.requires_auth
        ORDER BY s.url_context_root, ds.request_path, dbo.request_path
        """
    )
    if not rows:
        return []

    rest_view_names = [
        str(row.get("db_object_name", ""))
        for row in rows
        if str(row.get("db_schema_name", "")) == "restapidb" and str(row.get("object_kind", "")) == "VIEW" and str(row.get("db_object_name", ""))
    ]
    rest_routine_names = [
        str(row.get("db_object_name", ""))
        for row in rows
        if str(row.get("db_schema_name", "")) == "restapidb" and str(row.get("object_kind", "")) in {"PROCEDURE", "FUNCTION"} and str(row.get("db_object_name", ""))
    ]
    rest_view_map = _load_view_source_map("restapidb", rest_view_names)
    rest_routine_map = _load_routine_source_map("restapidb", rest_routine_names)

    results: list[dict[str, str]] = []
    for row in rows:
        service_path = str(row.get("service_path", "")).strip()
        schema_path = str(row.get("schema_path", "")).strip()
        object_path = str(row.get("object_path", "")).strip()
        object_kind = str(row.get("object_kind", "SCHEMA")).strip() or "SCHEMA"
        auth_apps = str(row.get("auth_apps", "")).strip()
        auth_app_name = normalize_auth_app(auth_apps)
        database_name = str(row.get("db_schema_name", "")).strip() or (schema_path.lstrip("/") or "-")
        source_object = str(row.get("db_object_name", "")).strip() or "-"

        if not service_path.startswith("/"):
            continue
        if schema_path and not schema_path.startswith("/"):
            schema_path = f"/{schema_path}"

        if database_name == "restapidb" and source_object != "-":
            if object_kind == "VIEW" and source_object in rest_view_map:
                database_name, source_object = rest_view_map[source_object]
            elif object_kind in {"PROCEDURE", "FUNCTION"} and source_object in rest_routine_map:
                database_name, source_object = rest_routine_map[source_object]

        endpoint = f"{service_path}{schema_path}{object_path}" if object_path else f"{service_path}{schema_path}"
        parameter_names = extract_endpoint_parameters(endpoint)
        results.append(
            {
                "endpoint": endpoint,
                "database": database_name or "-",
                "schema_path": schema_path or "/",
                "rest_schema_name": schema_path.lstrip("/") or "-",
                "service": service_path,
                "object_path": object_path or "-",
                "object_kind": object_kind,
                "source_object": source_object,
                "auth_required": str(row.get("auth_required", "Not Required")),
                "service_enabled": str(row.get("service_enabled", "-")),
                "schema_enabled": str(row.get("schema_enabled", "-")),
                "object_enabled": str(row.get("object_enabled", "-")),
                "auth_apps": auth_apps if auth_apps else "-",
                "auth_app_name": auth_app_name,
                "auth_path": str(row.get("auth_path", "/authentication")) or "/authentication",
                "param_names": parameter_names,
                "param_summary": ", ".join(parameter_names),
                "procedure_params": [],
            }
        )

    results.sort(key=lambda item: (item["service"], item["schema_path"], item["object_kind"], item["endpoint"]))
    return set_cached_value(cache_key, results)


def find_restapidb_service(endpoint_template: str, service_path: str, schema_name: str = "restapidb") -> dict[str, str] | None:
    for item in list_restapidb_services(schema_name):
        if item["endpoint"] == endpoint_template and item["service"] == service_path:
            return item
    return None


def execute_rest_endpoint(
    endpoint_template: str,
    *,
    query_string: str = "",
    auth_required: bool = False,
    object_kind: str = "VIEW",
    service_path: str = "",
    auth_path: str = "/authentication",
    auth_app: str = "",
    auth_username: str = "",
    auth_password: str = "",
    path_params: dict[str, str] | None = None,
    procedure_params: list[dict[str, str]] | None = None,
    procedure_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    endpoint_path = endpoint_template
    for name, value in (path_params or {}).items():
        endpoint_path = endpoint_path.replace(f"{{{name}}}", quote(value, safe=""))
        endpoint_path = endpoint_path.replace(f":{name}", quote(value, safe=""))

    ssl_context = ssl._create_unverified_context()
    runtime_config = get_runtime_config()

    def perform_request(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        redirects_remaining: int = 5,
    ) -> tuple[int, str, str]:
        request_obj = Request(url, headers=headers or {}, data=data, method=method)
        try:
            with urlopen(request_obj, context=ssl_context, timeout=max(runtime_config.connect_timeout * 4, 12)) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.getcode(), response.headers.get("Content-Type", ""), body
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and redirects_remaining > 0:
                location = exc.headers.get("Location", "").strip()
                if not location:
                    body = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
                if location.startswith("/"):
                    location = f"https://{runtime_config.api_host}:{runtime_config.api_port}{location}"
                next_method = method
                next_data = data
                if exc.code == 303:
                    next_method = "GET"
                    next_data = None
                return perform_request(
                    location,
                    method=next_method,
                    headers=headers,
                    data=next_data,
                    redirects_remaining=redirects_remaining - 1,
                )
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc

    try:
        headers: dict[str, str] = {}
        if auth_required:
            if not auth_username or not auth_password:
                raise RuntimeError("Authentication is required for this service.")

            normalized_auth_path = auth_path.strip() or "/authentication"
            if not normalized_auth_path.startswith("/"):
                normalized_auth_path = f"/{normalized_auth_path}"
            login_url = f"https://{runtime_config.api_host}:{runtime_config.api_port}{service_path}{normalized_auth_path}/login"
            payload = json.dumps(
                {
                    "username": auth_username,
                    "password": auth_password,
                    "authApp": auth_app or "MySQL",
                    "sessionType": "bearer",
                }
            ).encode("utf-8")
            try:
                login_status, _, login_body = perform_request(
                    login_url,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    data=payload,
                )
            except RuntimeError:
                fallback_login_url = f"{login_url}/"
                login_status, _, login_body = perform_request(
                    fallback_login_url,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    data=payload,
                )
            if login_status < 200 or login_status >= 300:
                raise RuntimeError(f"Authentication failed with HTTP {login_status}: {login_body}")
            login_doc = json.loads(login_body)
            token = str(login_doc.get("accessToken", "")).strip()
            if not token:
                raise RuntimeError(f"Authentication succeeded without accessToken: {login_doc}")
            headers["Authorization"] = f"Bearer {token}"

        url = f"https://{runtime_config.api_host}:{runtime_config.api_port}{endpoint_path}"
        if query_string:
            url = f"{url}?{query_string.lstrip('?')}"
        request_method = "GET"
        request_body: bytes | None = None
        if object_kind == "PROCEDURE":
            request_method = "POST"
            request_payload: dict[str, Any] = {}
            for parameter in procedure_params or []:
                mode = str(parameter.get("mode", "IN")).upper()
                name = str(parameter.get("name", "")).strip()
                if not name or mode == "OUT":
                    continue
                request_payload[name] = str((procedure_values or {}).get(name, ""))
            request_body = json.dumps(request_payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        status_code, content_type, body = perform_request(
            url,
            headers=headers,
            method=request_method,
            data=request_body,
        )
        parsed_body = parse_json_if_possible(body)
        normalized_body = normalize_rest_response(parsed_body, object_kind=object_kind)
        return {
            "status_code": status_code,
            "endpoint": endpoint_path,
            "query_string": query_string,
            "request_method": request_method,
            "request_payload": (procedure_values or {}) if object_kind == "PROCEDURE" else {},
            "content_type": content_type,
            "body": body,
            "json_body": parsed_body,
            "out_parameters": normalized_body["out_parameters"],
            "result_sets": normalized_body["result_sets"],
            "result_set_tables": build_result_set_tables(normalized_body["result_sets"]),
        }
    except URLError as exc:
        raise RuntimeError(f"REST request failed: {exc.reason}") from exc
