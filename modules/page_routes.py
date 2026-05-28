from __future__ import annotations

import hashlib
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from modules.catalog import OBJECT_PRIVILEGES, PAGE_CONTENT, USER_TABS, GRANT_TABS, RESTAPIDB_TABS
from modules.app_config import active_login_profile, default_login_profile
from modules.local_auth import (
    add_user_to_group,
    authenticate_local_user,
    assign_profile,
    assigned_profile_names,
    change_local_password,
    create_local_group,
    create_local_user,
    list_local_groups,
    list_local_users,
    list_profile_assignments,
    list_user_group_memberships,
    remove_profile_assignment,
)
from modules.profile_store import (
    LOCAL_ADMIN_PROFILE_NAME,
    delete_profile,
    get_profile,
    profile_names,
    rename_profile_assignments,
    update_profile,
)
from modules.profile_session_store import clear_profile_password, store_profile_password
from modules.update_service import poll_token_matches, read_update_status, start_update_job
from modules.services import (
    build_delete_rest_service_paths_definition,
    build_expose_database_to_service_definition,
    build_expose_existing_schema_procedure,
    build_rest_procedure_definition,
    build_rest_service_definition,
    build_rest_service_path_definition,
    classify_role,
    connection_dashboard_status,
    create_user_account,
    default_special_priv_category,
    delete_selected_users,
    ensure_login_tunnels,
    execute_rest_endpoint,
    fallback_user_role,
    fetch_grants,
    find_restapidb_service,
    get_rest_procedure_details,
    get_rest_service_auth_details,
    grant_object_privileges,
    grant_special_privileges,
    is_system_user,
    list_base_tables,
    list_databases,
    list_restapidb_objects,
    list_restapidb_services,
    list_rest_service_paths,
    list_schema_procedures,
    list_special_privileges,
    list_tables,
    list_users_with_roles,
    non_system_users,
    run_admin_connector_sql,
    run_admin_sql,
    special_privilege_categories,
    start_shared_tunnels,
    stop_all_shared_tunnels,
)
from modules.session_store import admin_subtabs, current_user, default_subtab, infer_initials, role_home, slug_allowed


def _can_access_rest_service_console(user: dict[str, Any] | None) -> bool:
    return bool(user and user["role"] in {"admin", "db_admin", "rest_admin", "test_user"})


def _can_manage_rest_api(user: dict[str, Any] | None) -> bool:
    return bool(user and user["role"] in {"admin", "db_admin", "rest_admin"})


def _can_manage_profile_database(user: dict[str, Any] | None) -> bool:
    return bool(user and user["role"] in {"admin", "db_admin"})


REST_API_PAGE_SLUGS = {
    "list-restful-services",
    "create-restful-service",
    "expose-db-as-service",
    "expose-table-as-service",
    "expose-sp-as-service",
}


REST_SQL_STATE_KEY = "rest_sql_workbench"


def _rest_sql_state(slug: str) -> dict[str, str]:
    state = session.get(REST_SQL_STATE_KEY, {})
    if not isinstance(state, dict):
        return {}
    item = state.get(slug, {})
    return item if isinstance(item, dict) else {}


def _store_rest_sql_state(slug: str, *, sql: str = "", result: str = "", error: str = "") -> None:
    state = session.get(REST_SQL_STATE_KEY, {})
    if not isinstance(state, dict):
        state = {}
    state[slug] = {
        "sql": sql,
        "result": result,
        "error": error,
    }
    session[REST_SQL_STATE_KEY] = state


def _execute_generated_rest_sql(*, slug: str, sql: str, redirect_args: dict[str, str]):
    try:
        output = run_admin_sql(sql, raw_output=True)
        result = str(output).strip() or "SQL executed successfully."
        _store_rest_sql_state(slug, sql=sql, result=result)
        flash("SQL executed with the active profile session.", "info")
    except Exception as exc:
        _store_rest_sql_state(slug, sql=sql, error=str(exc))
        flash(f"SQL execution failed: {exc}", "error")
    return redirect(url_for("dashboard", slug=slug, **redirect_args))


def _procedure_parameters_from_form() -> list[dict[str, str]]:
    param_names = request.form.getlist("param_name")
    param_types = request.form.getlist("param_type")
    param_modes = request.form.getlist("param_mode")
    parameters: list[dict[str, str]] = []
    for name, param_type, mode in zip(param_names, param_types, param_modes):
        if not name.strip() and not param_type.strip():
            continue
        parameters.append(
            {
                "name": name.strip(),
                "type": param_type.strip(),
                "mode": mode.strip().upper(),
            }
        )
    return parameters


def _parse_int_field(raw_value: str, *, label: str) -> int:
    try:
        value = int(str(raw_value).strip())
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a number.") from exc
    if value <= 0 or value > 65535:
        raise RuntimeError(f"{label} must be between 1 and 65535.")
    return value


def _parse_profile_name(raw_value: str) -> str:
    profile_name = str(raw_value or "").strip()
    if not profile_name:
        raise RuntimeError("Profile name is required.")
    if not all(char.isalnum() or char in {"-", "_"} for char in profile_name):
        raise RuntimeError("Profile name must use letters, numbers, dashes, or underscores only.")
    if profile_name == LOCAL_ADMIN_PROFILE_NAME:
        raise RuntimeError("The local admin socket profile is managed by setup and cannot be edited here.")
    return profile_name


def _build_login_profile(form_data) -> dict[str, Any]:
    profile_name = str(form_data.get("profile_name", "")).strip()
    profile = get_profile(profile_name)
    if not profile["db_host"]:
        raise RuntimeError("Target DB Host is required.")
    if profile["use_ssh_tunnel"]:
        if not profile["ssh_key_path"] or not profile["ssh_jump_host"] or not profile["ssh_jump_user"]:
            raise RuntimeError("SSH key path, jump host, and jump user are required when using SSH tunnel.")
        profile_signature = "|".join(
            [
                profile["ssh_jump_user"],
                profile["ssh_jump_host"],
                profile["db_host"],
                str(profile["db_port"]),
                profile.get("api_host", profile["db_host"]),
                str(profile["api_port"]),
                str(profile["local_db_port"]),
                str(profile["local_api_port"]),
            ]
        )
        profile["socket_name"] = f"webapp-{hashlib.sha1(profile_signature.encode('utf-8')).hexdigest()[:12]}.sock"
    else:
        profile["socket_name"] = ""
    return profile


def _build_config_profile(form_data) -> dict[str, Any]:
    return {
        "label": str(form_data.get("label", "")).strip(),
        "use_ssh_tunnel": form_data.get("use_ssh_tunnel") == "on",
        "db_host": str(form_data.get("db_host", "")).strip(),
        "db_port": _parse_int_field(form_data.get("db_port", "3306"), label="DB Port"),
        "api_host": str(form_data.get("api_host", "")).strip(),
        "api_port": _parse_int_field(form_data.get("api_port", "443"), label="REST API Port"),
        "local_db_port": _parse_int_field(form_data.get("local_db_port", "3306"), label="Local DB Port"),
        "local_api_port": _parse_int_field(form_data.get("local_api_port", "8443"), label="Local REST API Port"),
        "ssh_key_path": str(form_data.get("ssh_key_path", "")).strip(),
        "ssh_jump_host": str(form_data.get("ssh_jump_host", "")).strip(),
        "ssh_jump_user": str(form_data.get("ssh_jump_user", "")).strip(),
    }


def _visible_connection_profiles_for_login(user: dict[str, Any]) -> list[dict[str, str]]:
    public_profiles = [item for item in profile_names() if item["name"] != LOCAL_ADMIN_PROFILE_NAME]
    if user["role"] == "admin":
        return public_profiles
    assigned_names = assigned_profile_names(user["username"])
    return [item for item in public_profiles if item["name"] in assigned_names]


def _editable_connection_profiles() -> list[dict[str, str]]:
    return [item for item in profile_names() if item["name"] != LOCAL_ADMIN_PROFILE_NAME]


def _default_editable_profile_name() -> str:
    editable_profiles = _editable_connection_profiles()
    return editable_profiles[0]["name"] if editable_profiles else "default"


def _parse_grant_entry(grant: str) -> dict[str, str]:
    cleaned = grant.strip()
    upper = cleaned.upper()
    privilege = cleaned
    scope = "-"
    target = "-"
    options = ""
    category = "special"

    to_index = upper.find(" TO ")
    on_index = upper.find(" ON ")
    if cleaned.startswith("GRANT ") and to_index > 0:
        target = cleaned[to_index + 4 :].strip()
        if " WITH GRANT OPTION" in target.upper():
            marker_index = target.upper().find(" WITH GRANT OPTION")
            options = target[marker_index:].strip()
            target = target[:marker_index].strip()
        if on_index > 0 and on_index < to_index:
            privilege = cleaned[6:on_index].strip()
            scope = cleaned[on_index + 4 : to_index].strip()
            if scope == "*.*" or "@" in scope:
                category = "special"
            elif scope.endswith(".*"):
                category = "schema"
            else:
                category = "object"
        else:
            privilege = cleaned[6:to_index].strip()
            scope = "Role / Account"
            category = "special"

    return {
        "raw": cleaned,
        "privilege": privilege,
        "scope": scope,
        "target": target,
        "options": options,
        "category": category,
    }


def _build_grant_groups(grants: list[str]) -> list[dict[str, Any]]:
    grouped_items = {
        "special": [],
        "object": [],
        "schema": [],
    }
    for grant in grants:
        parsed = _parse_grant_entry(grant)
        grouped_items[parsed["category"]].append(parsed)

    return [
        {
            "slug": "special",
            "title": "Special Privilege",
            "summary": "Global privileges, administrative capabilities, and granted roles.",
            "items": grouped_items["special"],
        },
        {
            "slug": "object",
            "title": "Database Object",
            "summary": "Privileges granted on specific tables, views, procedures, or other individual objects.",
            "items": grouped_items["object"],
        },
        {
            "slug": "schema",
            "title": "Databases/Schema",
            "summary": "Privileges granted at the database or schema level.",
            "items": grouped_items["schema"],
        },
    ]


def login_redirect_response(message: str):
    stop_all_shared_tunnels()
    session.clear()
    flash(message, "error")
    return redirect(url_for("login"))


def login_json_response(message: str):
    stop_all_shared_tunnels()
    session.clear()
    return jsonify({"ok": False, "error": message, "redirect": url_for("login")}), 401


def _dashboard_error_response(*, slug: str, message: str, context: dict[str, Any]):
    flash(message, "error")
    return render_template("dashboard.html", **context)


def _dashboard_context_for_admin(slug: str) -> dict[str, Any]:
    active_tab = request.args.get("tab", default_subtab(slug))
    databases: list[str] = []
    tables: list[str] = []
    managed_users: list[dict[str, Any]] = []
    selected_db = request.args.get("db", "")
    special_privileges: list[dict[str, str]] = []
    special_priv_category = request.args.get("category", default_special_priv_category())
    restapidb_objects = {"tables": [], "views": [], "procedures": []}
    rest_services: list[dict[str, Any]] = []
    config_profile_name = request.args.get("profile", "").strip()
    if slug == "config" and config_profile_name == LOCAL_ADMIN_PROFILE_NAME:
        config_profile_name = ""
    if slug == "config" and not config_profile_name:
        config_profile_name = _default_editable_profile_name()
    config_profile = get_profile(config_profile_name)
    local_users = []
    local_groups = []
    user_group_memberships = []
    profile_assignments = []

    if slug == "config":
        active_tab = ""
        config_profile_name = config_profile["name"]
        local_users = list_local_users()
        local_groups = list_local_groups()
        user_group_memberships = list_user_group_memberships()
        profile_assignments = list_profile_assignments()
    if slug == "local-users":
        local_users = list_local_users()
    if slug == "local-groups":
        local_groups = list_local_groups()
        local_users = list_local_users()
        user_group_memberships = list_user_group_memberships()

    if slug == "user":
        managed_users = list_users_with_roles()
        if active_tab not in {tab["slug"] for tab in USER_TABS}:
            active_tab = default_subtab(slug)

    if slug == "granting-privileges":
        if active_tab not in {tab["slug"] for tab in GRANT_TABS}:
            active_tab = default_subtab(slug)
        if active_tab == "grant-object-priv":
            databases = list_databases()
            if selected_db and selected_db in databases:
                tables = list_tables(selected_db)
            else:
                selected_db = ""
            managed_users = non_system_users()
        elif active_tab == "special-priv":
            valid_categories = {item["slug"] for item in special_privilege_categories()}
            if special_priv_category not in valid_categories:
                special_priv_category = default_special_priv_category()
            special_privileges = list_special_privileges(special_priv_category)
            managed_users = non_system_users()

    if slug == "restapidb":
        if active_tab not in {tab["slug"] for tab in RESTAPIDB_TABS}:
            active_tab = default_subtab(slug)
        if active_tab == "list":
            restapidb_objects = list_restapidb_objects()
            rest_services = list_restapidb_services()

    return {
        "active_tab": active_tab,
        "subtabs": admin_subtabs(slug),
        "managed_users": managed_users,
        "databases": databases,
        "tables": tables,
        "selected_db": selected_db,
        "object_privileges": OBJECT_PRIVILEGES,
        "special_priv_categories": special_privilege_categories(),
        "special_privileges": special_privileges,
        "active_special_priv_category": special_priv_category,
        "restapidb_objects": restapidb_objects,
        "rest_services": rest_services,
        "config_profiles": _visible_connection_profiles_for_login(current_user() or {"role": "admin", "username": ""}),
        "editable_config_profiles": _editable_connection_profiles(),
        "config_profile": config_profile,
        "config_profile_name": config_profile_name,
        "local_users": local_users,
        "local_groups": local_groups,
        "user_group_memberships": user_group_memberships,
        "profile_assignments": profile_assignments,
    }


def _dashboard_context_for_status() -> dict[str, Any]:
    return {
        "dashboard_status": connection_dashboard_status(),
    }


def _dashboard_context_for_rest_admin(slug: str) -> dict[str, Any]:
    active_tab = request.args.get("tab", default_subtab(slug))
    selected_db = request.args.get("db", "").strip()
    selected_table = request.args.get("table", "").strip()
    service_name = request.args.get("service_name", "").strip()
    auth_required = request.args.get("auth_required", "Not Required").strip() or "Not Required"
    databases: list[str] = []
    tables: list[str] = []
    available_services: list[str] = []
    sys_procedures: list[str] = []
    selected_service = request.args.get("service_path", "").strip()
    procedure_name = request.args.get("procedure_name", "").strip()
    procedure_auth_required = request.args.get("procedure_auth_required", "Not Required").strip() or "Not Required"
    procedure_body = request.args.get("procedure_body", "SELECT * FROM information_schema.tables").strip()

    if slug == "create-restful-service":
        available_services = list_rest_service_paths()
        selected_db = ""
        selected_table = ""
    elif slug == "expose-db-as-service":
        databases = list_databases()
        available_services = list_rest_service_paths()
        if selected_service not in available_services:
            selected_service = available_services[0] if available_services else ""
        if selected_db and selected_db not in databases:
            selected_db = ""
    elif slug == "expose-table-as-service":
        databases = list_databases()
        available_services = list_rest_service_paths()
        if selected_service not in available_services:
            selected_service = available_services[0] if available_services else ""
        if selected_db and selected_db in databases:
            tables = list_base_tables(selected_db)
            if selected_table not in tables:
                selected_table = ""
        else:
            selected_db = ""
            selected_table = ""
    elif slug == "expose-sp-as-service":
        if active_tab not in {"user", "sys"}:
            active_tab = default_subtab(slug)
        available_services = list_rest_service_paths()
        if selected_service not in available_services:
            selected_service = available_services[0] if available_services else ""
        sys_procedures = list_schema_procedures("sys")

    service_slug = service_name.strip().lower().replace(" ", "-")
    service_slug = "".join(char for char in service_slug if char.isalnum() or char in {"-", "_"}).strip("-_")
    service_path = f"/{service_slug}" if service_slug else ""
    object_path = f"/{selected_table.lower()}" if selected_table else ""
    endpoint = f"{service_path}/restapidb{object_path}" if service_path and object_path else ""

    return {
        "active_tab": active_tab,
        "subtabs": admin_subtabs(slug),
        "databases": databases,
        "tables": tables,
        "selected_db": selected_db,
        "selected_table": selected_table,
        "rest_service_form": {
            "service_name": service_name,
            "auth_required": auth_required,
        },
        "rest_service_preview": {
            "service_path": service_path,
            "endpoint": service_path,
            "schema_path": "/restapidb",
        },
        "available_services": available_services,
        "rest_procedure_form": {
            "service_path": selected_service,
            "procedure_name": procedure_name,
            "auth_required": procedure_auth_required,
            "body": procedure_body or "SELECT * FROM information_schema.tables",
            "parameters": [
                {
                    "name": "",
                    "type": "VARCHAR(255)",
                    "mode": "IN",
                }
            ],
        },
        "rest_procedure_preview": {
            "endpoint": f"{selected_service}/restapidb/{procedure_name.lower()}" if selected_service and procedure_name else "",
            "schema_path": "/restapidb",
        },
        "sys_procedures": sys_procedures,
        "selected_service_path": selected_service,
        "rest_db_form": {
            "service_path": selected_service,
            "auth_required": auth_required,
        },
        "rest_db_preview": {
            "service_path": selected_service,
            "schema_path": f"/{selected_db.lower()}" if selected_db else "",
            "endpoint": f"{selected_service}/{selected_db.lower()}" if selected_service and selected_db else "",
        },
        "rest_table_form": {
            "service_path": selected_service,
            "auth_required": auth_required,
        },
        "rest_table_preview": {
            "service_path": selected_service,
            "schema_path": "/restapidb",
            "endpoint": f"{selected_service}/restapidb/{selected_table.lower()}" if selected_service and selected_table else "",
        },
        "rest_sql_workbench": _rest_sql_state(slug),
    }


def register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        return redirect(url_for("dashboard", slug=role_home(user["role"])))

    @app.post("/admin/config/profile")
    def admin_update_config_profile():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        original_profile_name = request.form.get("original_profile_name", "").strip()
        action = request.form.get("action", "update").strip()
        try:
            if action == "delete":
                profile_name = _parse_profile_name(original_profile_name)
                if not original_profile_name:
                    raise RuntimeError("Select a saved profile before deleting.")
                if len(_editable_connection_profiles()) <= 1:
                    raise RuntimeError("At least one DB profile must remain.")
                if not delete_profile(original_profile_name):
                    raise RuntimeError("Profile was not deleted.")
                flash(f"Deleted profile {original_profile_name}.", "info")
                return redirect(url_for("dashboard", slug="config", profile=_default_editable_profile_name()))

            profile_name = _parse_profile_name(request.form.get("profile_name", ""))
            updated = update_profile(profile_name, _build_config_profile(request.form))
            if action == "update" and original_profile_name and original_profile_name != profile_name:
                rename_profile_assignments(original_profile_name, profile_name)
                delete_profile(original_profile_name)
            if session.get("connection_profile", {}).get("name") == updated["name"]:
                session["connection_profile"] = updated
            flash(f"Saved profile {updated['label']}.", "info")
        except Exception as exc:
            flash(f"Configuration update failed: {exc}", "error")
            profile_name = original_profile_name or request.form.get("profile_name", "").strip() or _default_editable_profile_name()
        return redirect(url_for("dashboard", slug="config", profile=profile_name))

    @app.post("/admin/local-users/create")
    def admin_create_local_user():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        is_admin = request.form.get("is_admin") == "on"
        if not username or not password:
            flash("Username and temporary password are required.", "error")
        else:
            try:
                create_local_user(username, password, is_admin=is_admin, display_name=display_name)
                flash(f"Created local user {username}.", "info")
            except Exception as exc:
                flash(f"Local user creation failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="local-users"))

    @app.post("/admin/local-groups/create")
    def admin_create_local_group():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        group_name = request.form.get("group_name", "").strip()
        display_name = request.form.get("display_name", "").strip()
        is_admin = request.form.get("is_admin") == "on"
        if not group_name:
            flash("Group name is required.", "error")
        else:
            try:
                create_local_group(group_name, is_admin=is_admin, display_name=display_name)
                flash(f"Saved local group {group_name}.", "info")
            except Exception as exc:
                flash(f"Local group save failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="local-groups"))

    @app.post("/admin/local-groups/add-user")
    def admin_add_user_to_group():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        username = request.form.get("username", "").strip()
        group_name = request.form.get("group_name", "").strip()
        if not username or not group_name:
            flash("User and group are required.", "error")
        else:
            try:
                add_user_to_group(username, group_name)
                flash(f"Added {username} to {group_name}.", "info")
            except Exception as exc:
                flash(f"Group assignment failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="local-groups"))

    @app.post("/admin/profile-assignments/create")
    def admin_assign_profile():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        raw_profile_name = request.form.get("profile_name", "").strip()
        subject_type = request.form.get("subject_type", "").strip()
        subject_name = request.form.get("subject_name", "").strip()
        if not raw_profile_name or not subject_type or not subject_name:
            flash("Profile assignment requires profile, target type, and target name.", "error")
        else:
            try:
                profile_name = _parse_profile_name(raw_profile_name)
                editable_names = {item["name"] for item in _editable_connection_profiles()}
                if profile_name not in editable_names:
                    raise RuntimeError("Profile does not exist.")
                assign_profile(profile_name, subject_type, subject_name)
                flash(f"Assigned {profile_name} to {subject_type} {subject_name}.", "info")
            except Exception as exc:
                flash(f"Profile assignment failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="config"))

    @app.post("/admin/profile-assignments/delete")
    def admin_remove_profile_assignment():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        raw_profile_name = request.form.get("profile_name", "").strip()
        subject_type = request.form.get("subject_type", "").strip()
        subject_name = request.form.get("subject_name", "").strip()
        if not raw_profile_name or not subject_type or not subject_name:
            flash("Profile assignment removal requires profile, target type, and target name.", "error")
        else:
            try:
                profile_name = _parse_profile_name(raw_profile_name)
                if remove_profile_assignment(profile_name, subject_type, subject_name):
                    flash(f"Removed {profile_name} from {subject_type} {subject_name}.", "info")
                else:
                    flash("Profile assignment was not found.", "error")
            except Exception as exc:
                flash(f"Profile assignment removal failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="config"))

    @app.post("/admin/update/start")
    def admin_start_update():
        user = current_user()
        if not user or user["role"] != "admin":
            return redirect(url_for("login"))
        status = start_update_job()
        session["update_poll_token"] = status.get("poll_token", "")
        flash("Auto-update started.", "info")
        return redirect(url_for("dashboard", slug="update"))

    @app.get("/admin/update/status")
    def admin_update_status():
        user = current_user()
        token = request.headers.get("X-MySQL-Rest-Console-Update-Poll-Token", "")
        if not user and not poll_token_matches(token):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return jsonify(read_update_status())

    @app.post("/admin/users/create")
    def admin_create_user():
        user = current_user()
        if not _can_manage_profile_database(user):
            return redirect(url_for("login"))

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role_name = request.form.get("role_name", "Rest User")

        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm_password:
            flash("Password confirmation does not match.", "error")
        elif is_system_user(username):
            flash("That username is reserved as a system user.", "error")
        else:
            try:
                create_user_account(username, password, role_name)
                flash(f"User {username} created as {role_name}.", "info")
            except Exception as exc:
                return login_redirect_response(f"Create user failed: {exc}")

        return redirect(url_for("dashboard", slug="user", tab="create"))

    @app.post("/admin/users/delete")
    def admin_delete_users():
        user = current_user()
        if not _can_manage_profile_database(user):
            return redirect(url_for("login"))

        selected_keys = request.form.getlist("selected_users")
        if not selected_keys:
            flash("Select at least one non-system user to delete.", "error")
            return redirect(url_for("dashboard", slug="user", tab="list"))

        try:
            dropped = delete_selected_users(selected_keys)
            flash(f"Deleted {dropped} user(s).", "info")
        except Exception as exc:
            return login_redirect_response(f"Delete failed: {exc}")

        return redirect(url_for("dashboard", slug="user", tab="list"))

    @app.post("/admin/grants/object")
    def admin_grant_object_priv():
        user = current_user()
        if not _can_manage_profile_database(user):
            return redirect(url_for("login"))

        schema_name = request.form.get("schema_name", "").strip()
        table_name = request.form.get("table_name", "").strip()
        privilege = request.form.get("privilege", "SELECT")
        selected_keys = request.form.getlist("selected_users")

        if not schema_name or not table_name or not selected_keys:
            flash("Select a database, table, privilege, and at least one user.", "error")
            return redirect(url_for("dashboard", slug="granting-privileges", tab="grant-object-priv", db=schema_name or ""))

        try:
            granted = grant_object_privileges(schema_name, table_name, privilege, selected_keys)
            flash(f"Granted {privilege} on {schema_name}.{table_name} to {granted} user(s).", "info")
        except Exception as exc:
            return login_redirect_response(f"Grant failed: {exc}")

        return redirect(url_for("dashboard", slug="granting-privileges", tab="grant-object-priv", db=schema_name))

    @app.post("/admin/grants/special")
    def admin_grant_special_priv():
        user = current_user()
        if not _can_manage_profile_database(user):
            return redirect(url_for("login"))

        category = request.form.get("category", default_special_priv_category()).strip()
        selected_items = request.form.getlist("selected_privileges")
        selected_keys = request.form.getlist("selected_users")

        if not selected_items or not selected_keys:
            flash("Select at least one special privilege and one target user.", "error")
            return redirect(url_for("dashboard", slug="granting-privileges", tab="special-priv", category=category))

        try:
            selected_count, granted_users = grant_special_privileges(selected_items, selected_keys)
            flash(f"Granted {selected_count} special privilege item(s) to {granted_users} user(s).", "info")
        except Exception as exc:
            return login_redirect_response(f"Special privilege grant failed: {exc}")

        return redirect(url_for("dashboard", slug="granting-privileges", tab="special-priv", category=category))

    @app.post("/admin/restapidb/test")
    def admin_test_restapidb_service():
        user = current_user()
        if not _can_access_rest_service_console(user):
            return login_json_response("Session expired. Please log in again.")

        payload = request.get_json(silent=True) or {}
        endpoint_template = str(payload.get("endpoint_template", "")).strip()
        service_path = str(payload.get("service_path", "")).strip()
        query_string = str(payload.get("query_string", "")).strip()
        path_params = payload.get("path_params", {}) or {}
        procedure_values = payload.get("procedure_values", {}) or {}

        if not endpoint_template or not service_path:
            return jsonify({"ok": False, "error": "REST test request was incomplete."}), 400

        service_item = find_restapidb_service(endpoint_template, service_path)
        if service_item is None:
            return jsonify({"ok": False, "error": "REST service metadata could not be resolved."}), 400

        try:
            auth_details = get_rest_service_auth_details(
                service_item["service"],
                schema_path=service_item.get("schema_path", "/restapidb"),
                auth_apps=service_item.get("auth_apps", ""),
            )
            procedure_params = service_item.get("procedure_params", [])
            if service_item.get("object_kind") in {"PROCEDURE", "FUNCTION"} and not procedure_params:
                procedure_params = get_rest_procedure_details(
                    service_path=service_item["service"],
                    schema_path=service_item.get("schema_path", "/restapidb"),
                    object_path=service_item.get("object_path", "/"),
                ).get("procedure_params", [])
            result = execute_rest_endpoint(
                endpoint_template,
                query_string=query_string,
                auth_required=auth_details["auth_required"] == "Required",
                object_kind=service_item.get("object_kind", "VIEW"),
                service_path=service_path,
                auth_path=auth_details.get("auth_path", "/authentication"),
                auth_app=auth_details.get("auth_app_name", service_item.get("auth_app_name", "")),
                auth_username=str(payload.get("auth_username", "")).strip(),
                auth_password=str(payload.get("auth_password", "")),
                path_params={str(key): str(value) for key, value in path_params.items()},
                procedure_params=procedure_params,
                procedure_values={str(key): str(value) for key, value in procedure_values.items()},
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"REST test failed: {exc}"}), 400

        return jsonify({"ok": True, **result})

    @app.get("/admin/restapidb/services")
    def admin_restapidb_services():
        user = current_user()
        if not _can_access_rest_service_console(user):
            return login_json_response("Session expired. Please log in again.")
        try:
            services = list_restapidb_services()
            return jsonify({"ok": True, "services": services})
        except Exception as exc:
            return login_json_response(f"REST service discovery failed: {exc}")

    @app.get("/admin/restapidb/service-detail")
    def admin_restapidb_service_detail():
        user = current_user()
        if not _can_access_rest_service_console(user):
            return login_json_response("Session expired. Please log in again.")

        service_path = request.args.get("service_path", "").strip()
        schema_path = request.args.get("schema_path", "").strip()
        object_path = request.args.get("object_path", "").strip()
        object_kind = request.args.get("object_kind", "").strip().upper()

        if not service_path or not schema_path or not object_path or object_kind not in {"PROCEDURE", "FUNCTION"}:
            return jsonify({"ok": False, "error": "REST service detail request was incomplete."}), 400

        try:
            details = get_rest_procedure_details(
                service_path=service_path,
                schema_path=schema_path,
                object_path=object_path,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"REST service detail failed: {exc}"}), 400

        return jsonify({"ok": True, **details})

    @app.post("/rest-admin/services/create")
    def rest_admin_create_restful_service():
        user = current_user()
        if not _can_manage_rest_api(user):
            return redirect(url_for("login"))

        service_name = request.form.get("service_name", "").strip()
        action = request.form.get("action", "generate")

        if action == "execute" and request.form.get("sql"):
            return _execute_generated_rest_sql(
                slug="create-restful-service",
                sql=request.form.get("sql", ""),
                redirect_args={"service_name": service_name},
            )

        if action in {"delete-selected", "delete-one"}:
            selected_paths = request.form.getlist("selected_service_paths")
            single_path = request.form.get("service_path", "").strip()
            if single_path:
                selected_paths = [single_path]
            try:
                existing_paths = set(list_rest_service_paths())
                selected_paths = [path for path in selected_paths if path in existing_paths]
                result = build_delete_rest_service_paths_definition(service_paths=selected_paths)
                _store_rest_sql_state("create-restful-service", sql=result["sql"])
                flash(f"Generated delete SQL for {result['service_paths']}.", "info")
            except Exception as exc:
                flash(f"REST service delete SQL generation failed: {exc}", "error")
            return redirect(url_for("dashboard", slug="create-restful-service", service_name=service_name))

        if not service_name:
            flash("Enter the REST service path name.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="create-restful-service",
                    service_name=service_name,
                )
            )

        try:
            result = build_rest_service_path_definition(service_name=service_name)
            _store_rest_sql_state("create-restful-service", sql=result["sql"])
            flash("Generated REST service SQL.", "info")
        except Exception as exc:
            flash(f"REST service SQL generation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="create-restful-service",
                    service_name=service_name,
                )
            )

        return redirect(url_for("dashboard", slug="create-restful-service", service_name=service_name))

    @app.post("/rest-admin/schemas/create")
    def rest_admin_expose_database_service():
        user = current_user()
        if not _can_manage_rest_api(user):
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        source_schema = request.form.get("source_schema", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"
        action = request.form.get("action", "generate")

        if not service_path or not source_schema:
            flash("Choose a REST service and database to expose.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-db-as-service",
                    service_path=service_path,
                    db=source_schema,
                    auth_required="Required" if auth_required else "Not Required",
                )
            )

        try:
            result = build_expose_database_to_service_definition(
                service_path=service_path,
                source_schema=source_schema,
                auth_required=auth_required,
            )
            redirect_args = {
                "service_path": service_path,
                "db": source_schema,
                "auth_required": "Required" if auth_required else "Not Required",
            }
            if action == "execute":
                return _execute_generated_rest_sql(
                    slug="expose-db-as-service",
                    sql=request.form.get("sql", result["sql"]),
                    redirect_args=redirect_args,
                )
            _store_rest_sql_state("expose-db-as-service", sql=result["sql"])
            flash("Generated database exposure SQL.", "info")
        except Exception as exc:
            flash(f"Database exposure SQL generation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-db-as-service",
                    service_path=service_path,
                    db=source_schema,
                    auth_required="Required" if auth_required else "Not Required",
                )
            )

        return redirect(url_for("dashboard", slug="expose-db-as-service", **redirect_args))

    @app.post("/rest-admin/tables/create")
    def rest_admin_expose_table_service():
        user = current_user()
        if not _can_manage_rest_api(user):
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        source_schema = request.form.get("source_schema", "").strip()
        source_table = request.form.get("source_table", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"
        action = request.form.get("action", "generate")

        if not service_path or not source_schema or not source_table:
            flash("Choose a REST service, database, and table to expose.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-table-as-service",
                    service_path=service_path,
                    db=source_schema,
                    table=source_table,
                    auth_required="Required" if auth_required else "Not Required",
                )
            )

        try:
            result = build_rest_service_definition(
                service_path=service_path,
                source_schema=source_schema,
                source_table=source_table,
                auth_required=auth_required,
            )
            redirect_args = {
                "service_path": service_path,
                "db": source_schema,
                "table": source_table,
                "auth_required": "Required" if auth_required else "Not Required",
            }
            if action == "execute":
                return _execute_generated_rest_sql(
                    slug="expose-table-as-service",
                    sql=request.form.get("sql", result["sql"]),
                    redirect_args=redirect_args,
                )
            _store_rest_sql_state("expose-table-as-service", sql=result["sql"])
            flash("Generated table exposure SQL.", "info")
        except Exception as exc:
            flash(f"Table exposure SQL generation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-table-as-service",
                    service_path=service_path,
                    db=source_schema,
                    table=source_table,
                    auth_required="Required" if auth_required else "Not Required",
                )
            )

        return redirect(url_for("dashboard", slug="expose-table-as-service", **redirect_args))

    @app.post("/rest-admin/procedures/create")
    def rest_admin_create_rest_procedure():
        user = current_user()
        if not _can_manage_rest_api(user):
            return redirect(url_for("login"))

        procedure_name = request.form.get("procedure_name", "").strip()
        service_path = request.form.get("service_path", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"
        body_sql = request.form.get("body_sql", "").strip()
        action = request.form.get("action", "generate")
        parameters = _procedure_parameters_from_form()

        if not procedure_name or not service_path or not body_sql:
            flash("Enter the procedure name, choose a REST service, and provide the SQL body.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-sp-as-service",
                    service_path=service_path,
                    procedure_name=procedure_name,
                    procedure_auth_required="Required" if auth_required else "Not Required",
                    procedure_body=body_sql,
                )
            )

        try:
            result = build_rest_procedure_definition(
                procedure_name=procedure_name,
                service_path=service_path,
                auth_required=auth_required,
                parameters=parameters,
                body_sql=body_sql,
            )
            redirect_args = {
                "tab": "user",
                "service_path": service_path,
                "procedure_name": procedure_name,
                "procedure_auth_required": "Required" if auth_required else "Not Required",
                "procedure_body": body_sql,
            }
            if action == "execute":
                return _execute_generated_rest_sql(
                    slug="expose-sp-as-service",
                    sql=request.form.get("sql", result["sql"]),
                    redirect_args=redirect_args,
                )
            _store_rest_sql_state("expose-sp-as-service", sql=result["sql"])
            flash("Generated REST procedure SQL.", "info")
        except Exception as exc:
            flash(f"REST procedure SQL generation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-sp-as-service",
                    tab="user",
                    service_path=service_path,
                    procedure_name=procedure_name,
                    procedure_auth_required="Required" if auth_required else "Not Required",
                    procedure_body=body_sql,
                )
            )

        return redirect(url_for("dashboard", slug="expose-sp-as-service", **redirect_args))

    @app.post("/rest-admin/procedures/expose-sys")
    def rest_admin_expose_sys_procedure():
        user = current_user()
        if not _can_manage_rest_api(user):
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        procedure_name = request.form.get("procedure_name", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"
        action = request.form.get("action", "generate")

        if action == "execute" and request.form.get("sql"):
            return _execute_generated_rest_sql(
                slug="expose-sp-as-service",
                sql=request.form.get("sql", ""),
                redirect_args={
                    "tab": "sys",
                    "service_path": service_path,
                    "procedure_auth_required": "Required" if auth_required else "Not Required",
                },
            )

        if not service_path or not procedure_name:
            flash("Choose a REST service and a SYS stored procedure to expose.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-sp-as-service",
                    tab="sys",
                    service_path=service_path,
                )
            )

        try:
            result = build_expose_existing_schema_procedure(
                source_schema="sys",
                procedure_name=procedure_name,
                service_path=service_path,
                auth_required=auth_required,
            )
            redirect_args = {
                "tab": "sys",
                "service_path": service_path,
                "procedure_auth_required": "Required" if auth_required else "Not Required",
            }
            if action == "execute":
                return _execute_generated_rest_sql(
                    slug="expose-sp-as-service",
                    sql=request.form.get("sql", result["sql"]),
                    redirect_args=redirect_args,
                )
            _store_rest_sql_state("expose-sp-as-service", sql=result["sql"])
            flash("Generated SYS procedure exposure SQL.", "info")
        except Exception as exc:
            flash(f"SYS procedure exposure SQL generation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-sp-as-service",
                    tab="sys",
                    service_path=service_path,
                    procedure_auth_required="Required" if auth_required else "Not Required",
                )
            )

        return redirect(url_for("dashboard", slug="expose-sp-as-service", **redirect_args))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or not password:
                return render_template(
                    "login.html",
                    login_error="Enter both username and password.",
                    login_username=username,
                )
            local_user = authenticate_local_user(username, password)
            if not local_user:
                return render_template("login.html", login_error="Login failed.", login_username=username)
            role = "admin" if local_user["is_admin"] else "local_user"
            session["username"] = username
            session["role"] = role
            session["local_role"] = role
            session["initials"] = infer_initials(username)
            session["grants"] = []
            session["local_login_complete"] = True
            session["tunnels_ready"] = True
            if local_user.get("force_password_change"):
                session["force_password_change"] = True
                return redirect(url_for("change_password"))
            return redirect(url_for("dashboard", slug=role_home(role)))

        user = current_user()
        if user and session.get("force_password_change"):
            return redirect(url_for("change_password"))
        if user:
            return redirect(url_for("dashboard", slug=role_home(user["role"])))
        return render_template("login.html")

    @app.route("/change-password", methods=["GET", "POST"])
    def change_password():
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if not password or password != confirm:
                flash("Password confirmation does not match.", "error")
            elif user["username"] == "localadmin" and password == "localadmin":
                flash("Choose a new password different from the bootstrap password.", "error")
            else:
                try:
                    change_local_password(user["username"], password)
                    session.clear()
                    flash("Password changed. Sign in again.", "info")
                    return redirect(url_for("login"))
                except Exception as exc:
                    flash(f"Password change failed: {exc}", "error")
        return render_template(
            "login.html",
            force_password_change=True,
            login_username=user["username"],
            page={
                "title": "Change Password",
                "summary": "Change the local bootstrap password before opening the console.",
            },
            initials=session.get("initials", infer_initials(user["username"])),
        )

    @app.post("/profiles/login")
    def profile_login():
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        profile_name = request.form.get("profile_name", "").strip()
        username = request.form.get("db_username", "").strip()
        password = request.form.get("db_password", "")
        if not profile_name or not username or not password:
            flash("Profile, DB username, and DB password are required.", "error")
            return redirect(url_for("dashboard", slug="profile-login"))
        if user["role"] != "admin" and profile_name not in assigned_profile_names(user["username"]):
            flash("That profile is not assigned to your user or group.", "error")
            return redirect(url_for("dashboard", slug="profile-login"))
        previous_state = {
            "username": session.get("username"),
            "initials": session.get("initials"),
            "connection_profile": session.get("connection_profile"),
            "db_username": session.get("db_username"),
            "db_role": session.get("db_role"),
            "role": session.get("role"),
            "local_role": session.get("local_role"),
            "local_login_complete": session.get("local_login_complete"),
            "grants": session.get("grants"),
            "profile_credential_token": session.get("profile_credential_token"),
        }
        try:
            login_profile = _build_login_profile({"profile_name": profile_name})
            previous_profile = previous_state["connection_profile"]
            if previous_profile and previous_profile != login_profile:
                stop_all_shared_tunnels()
            session["connection_profile"] = login_profile
            if login_profile.get("use_ssh_tunnel"):
                start_shared_tunnels()
            ensure_login_tunnels()
            grants = fetch_grants(username, password)
            db_role = classify_role(username, grants)
            session["db_username"] = username
            session["db_role"] = db_role
            clear_profile_password(str(session.get("profile_credential_token", "")))
            session["profile_credential_token"] = store_profile_password(password)
            session["username"] = username
            session["initials"] = infer_initials(username)
            session["role"] = "db_admin" if db_role == "admin" else db_role
            session.pop("local_role", None)
            session.pop("local_login_complete", None)
            session["grants"] = grants
            flash(f"Connected to profile {login_profile['label']}.", "info")
            return redirect(url_for("dashboard", slug=role_home(session["role"])))
        except Exception as exc:
            clear_profile_password(str(session.get("profile_credential_token", "")))
            for key, value in previous_state.items():
                if value is None:
                    session.pop(key, None)
                else:
                    session[key] = value
            flash(f"Profile login failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="profile-login"))

    @app.route("/logout", methods=["POST"])
    def logout():
        clear_profile_password(str(session.get("profile_credential_token", "")))
        stop_all_shared_tunnels()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/dashboard/<slug>")
    def dashboard(slug: str):
        user = current_user()
        if not user:
            return redirect(url_for("login"))

        if not session.get("tunnels_ready"):
            return login_redirect_response("Tunnel session expired. Please log in again.")

        if not slug_allowed(user["role"], slug):
            flash("That page is not available for your role.", "error")
            return redirect(url_for("dashboard", slug=role_home(user["role"])))

        page = PAGE_CONTENT.get(slug)
        if not page:
            flash("Unknown page.", "error")
            return redirect(url_for("dashboard", slug=role_home(user["role"])))

        context = {
            "page": page,
            "grants": session.get("grants", []),
            "grant_groups": _build_grant_groups(session.get("grants", [])),
            "initials": session.get("initials", "HW"),
            "login_notice": session.pop("login_notice", None),
            "active_tab": default_subtab(slug),
            "subtabs": admin_subtabs(slug),
            "managed_users": [],
            "databases": [],
            "tables": [],
            "selected_db": "",
            "object_privileges": OBJECT_PRIVILEGES,
            "special_priv_categories": special_privilege_categories(),
            "special_privileges": [],
            "active_special_priv_category": default_special_priv_category(),
            "restapidb_objects": {"tables": [], "views": [], "procedures": []},
            "rest_services": [],
            "available_services": [],
            "rest_service_form": {
                "service_name": "",
                "auth_required": "Not Required",
            },
            "rest_service_preview": {
                "service_path": "",
                "endpoint": "",
                "schema_path": "/restapidb",
            },
            "rest_db_form": {
                "service_path": "",
                "auth_required": "Not Required",
            },
            "rest_db_preview": {
                "service_path": "",
                "schema_path": "",
                "endpoint": "",
            },
            "rest_table_form": {
                "service_path": "",
                "auth_required": "Not Required",
            },
            "rest_table_preview": {
                "service_path": "",
                "schema_path": "/restapidb",
                "endpoint": "",
            },
            "rest_procedure_form": {
                "service_path": "",
                "procedure_name": "",
                "auth_required": "Not Required",
                "body": "SELECT * FROM information_schema.tables",
                "parameters": [
                    {
                        "name": "",
                        "type": "VARCHAR(255)",
                        "mode": "IN",
                    }
                ],
            },
            "rest_procedure_preview": {
                "endpoint": "",
                "schema_path": "/restapidb",
            },
            "sys_procedures": [],
            "selected_service_path": "",
            "config_profiles": _visible_connection_profiles_for_login(user),
            "editable_config_profiles": _editable_connection_profiles(),
            "config_profile": get_profile(_default_editable_profile_name()),
            "config_profile_name": _default_editable_profile_name(),
            "update_status": read_update_status(),
            "update_poll_token": session.get("update_poll_token", ""),
            "local_users": [],
            "local_groups": [],
            "user_group_memberships": [],
            "profile_assignments": [],
            "dashboard_status": {},
            "hide_hero_panel": slug in {
                "dashboard",
                "user",
                "granting-privileges",
                "restapidb",
                "list-restful-services",
                "create-restful-service",
                "expose-db-as-service",
                "expose-table-as-service",
                "expose-sp-as-service",
            }
            or (slug == "show-grants" and bool(session.get("connection_profile"))),
        }

        if slug == "dashboard":
            try:
                context.update(_dashboard_context_for_status())
            except Exception as exc:
                context["dashboard_status"] = {
                    "db_status": f"Failed: {exc}",
                    "db_version": "-",
                    "mysqlsh_path": "-",
                    "mysqlsh_version": "-",
                    "restful_enabled": "Unknown",
                    "restful_detail": "-",
                }

        if user["role"] == "admin":
            try:
                context.update(_dashboard_context_for_admin(slug))
            except Exception as exc:
                if slug == "user":
                    return login_redirect_response(f"Session failed while loading users: {exc}")
                if slug == "granting-privileges":
                    return login_redirect_response(f"Session failed while loading grants: {exc}")
                if slug == "restapidb":
                    return login_redirect_response(f"Session failed while loading RestAPIDB: {exc}")
                return login_redirect_response(f"Session failed while loading page: {exc}")

        if user["role"] == "db_admin" and slug in {"user", "granting-privileges", "restapidb"}:
            try:
                context.update(_dashboard_context_for_admin(slug))
            except Exception as exc:
                if slug == "user":
                    return login_redirect_response(f"Session failed while loading users: {exc}")
                if slug == "granting-privileges":
                    return login_redirect_response(f"Session failed while loading grants: {exc}")
                if slug == "restapidb":
                    return login_redirect_response(f"Session failed while loading RestAPIDB: {exc}")
                return login_redirect_response(f"Session failed while loading page: {exc}")

        if user["role"] in {"admin", "db_admin", "rest_admin"} and slug in REST_API_PAGE_SLUGS:
            try:
                context.update(_dashboard_context_for_rest_admin(slug))
            except Exception as exc:
                return _dashboard_error_response(
                    slug=slug,
                    message=f"Rest Admin page failed to load: {exc}",
                    context=context,
                )

        if user["role"] in {"admin", "db_admin", "rest_admin", "test_user"} and slug == "list-restful-services":
            context["subtabs"] = []
            try:
                context["rest_services"] = list_restapidb_services()
            except Exception as exc:
                return _dashboard_error_response(
                    slug=slug,
                    message=f"REST services failed to load: {exc}",
                    context=context,
                )

        return render_template("dashboard.html", **context)
