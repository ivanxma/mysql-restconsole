from __future__ import annotations

import hashlib
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from modules.catalog import OBJECT_PRIVILEGES, PAGE_CONTENT, USER_TABS, GRANT_TABS, RESTAPIDB_TABS
from modules.app_config import active_login_profile, default_login_profile
from modules.profile_store import can_manage_profiles, get_profile, profile_names, update_profile
from modules.update_service import poll_token_matches, read_update_status, start_update_job
from modules.services import (
    classify_role,
    create_user_account,
    create_rest_procedure_definition,
    create_rest_service_path_definition,
    create_rest_service_definition,
    default_special_priv_category,
    delete_selected_users,
    ensure_login_tunnels,
    execute_rest_endpoint,
    expose_database_to_service_definition,
    expose_existing_schema_procedure,
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
    special_privilege_categories,
    start_shared_tunnels,
    stop_all_shared_tunnels,
)
from modules.session_store import admin_subtabs, current_user, default_subtab, infer_initials, role_home, slug_allowed


def _can_access_rest_service_console(user: dict[str, Any] | None) -> bool:
    return bool(user and user["role"] in {"admin", "rest_admin", "test_user"})


def _parse_int_field(raw_value: str, *, label: str) -> int:
    try:
        value = int(str(raw_value).strip())
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a number.") from exc
    if value <= 0 or value > 65535:
        raise RuntimeError(f"{label} must be between 1 and 65535.")
    return value


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
    config_profile = get_profile(config_profile_name)

    if slug == "config":
        active_tab = ""
        config_profile_name = config_profile["name"]

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
        "config_profiles": profile_names(),
        "config_profile": config_profile,
        "config_profile_name": config_profile_name,
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
        if not can_manage_profiles(session.get("connection_profile", {})):
            flash("Profile management requires local-admin-profile.", "error")
            return redirect(url_for("dashboard", slug=role_home(user["role"])))

        profile_name = request.form.get("profile_name", "").strip() or "default"
        try:
            updated = update_profile(profile_name, _build_config_profile(request.form))
            if session.get("connection_profile", {}).get("name") == updated["name"]:
                session["connection_profile"] = updated
            flash(f"Updated profile {updated['label']}.", "info")
        except Exception as exc:
            flash(f"Configuration update failed: {exc}", "error")
        return redirect(url_for("dashboard", slug="config", profile=profile_name))

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
        if not user or user["role"] != "admin":
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
        if not user or user["role"] != "admin":
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
        if not user or user["role"] != "admin":
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
        if not user or user["role"] != "admin":
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
        if not user or user["role"] != "rest_admin":
            return redirect(url_for("login"))

        service_name = request.form.get("service_name", "").strip()

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
            result = create_rest_service_path_definition(
                service_name=service_name,
            )
            flash(
                f"Created REST service path {result['service_path']}.",
                "info",
            )
        except Exception as exc:
            flash(f"REST service creation failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="create-restful-service",
                    service_name=service_name,
                )
            )

        return redirect(url_for("dashboard", slug="list-restful-services"))

    @app.post("/rest-admin/schemas/create")
    def rest_admin_expose_database_service():
        user = current_user()
        if not user or user["role"] != "rest_admin":
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        source_schema = request.form.get("source_schema", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"

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
            result = expose_database_to_service_definition(
                service_path=service_path,
                source_schema=source_schema,
                auth_required=auth_required,
            )
            flash(
                f"Exposed database {result['source_schema']} on {result['service_path']} as {result['schema_path']} ({result['auth_required']}).",
                "info",
            )
        except Exception as exc:
            flash(f"Database exposure failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-db-as-service",
                    service_path=service_path,
                    db=source_schema,
                    auth_required="Required" if auth_required else "Not Required",
                )
            )

        return redirect(url_for("dashboard", slug="list-restful-services"))

    @app.post("/rest-admin/tables/create")
    def rest_admin_expose_table_service():
        user = current_user()
        if not user or user["role"] != "rest_admin":
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        source_schema = request.form.get("source_schema", "").strip()
        source_table = request.form.get("source_table", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"

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
            result = create_rest_service_definition(
                service_path=service_path,
                source_schema=source_schema,
                source_table=source_table,
                auth_required=auth_required,
            )
            flash(
                f"Exposed {result['source_table']} through {result['endpoint']} ({result['auth_required']}).",
                "info",
            )
        except Exception as exc:
            flash(f"Table exposure failed: {exc}", "error")
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

        return redirect(url_for("dashboard", slug="list-restful-services"))

    @app.post("/rest-admin/procedures/create")
    def rest_admin_create_rest_procedure():
        user = current_user()
        if not user or user["role"] != "rest_admin":
            return redirect(url_for("login"))

        procedure_name = request.form.get("procedure_name", "").strip()
        service_path = request.form.get("service_path", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"
        body_sql = request.form.get("body_sql", "").strip()
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
            result = create_rest_procedure_definition(
                procedure_name=procedure_name,
                service_path=service_path,
                auth_required=auth_required,
                parameters=parameters,
                body_sql=body_sql,
            )
            flash(
                f"Created stored procedure restapidb.{result['procedure_name']} and exposed {result['endpoint']} ({result['auth_required']}).",
                "info",
            )
        except Exception as exc:
            flash(f"REST procedure creation failed: {exc}", "error")
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

        return redirect(url_for("dashboard", slug="list-restful-services"))

    @app.post("/rest-admin/procedures/expose-sys")
    def rest_admin_expose_sys_procedure():
        user = current_user()
        if not user or user["role"] != "rest_admin":
            return redirect(url_for("login"))

        service_path = request.form.get("service_path", "").strip()
        procedure_name = request.form.get("procedure_name", "").strip()
        auth_required = request.form.get("auth_required", "Not Required") == "Required"

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
            result = expose_existing_schema_procedure(
                source_schema="sys",
                procedure_name=procedure_name,
                service_path=service_path,
                auth_required=auth_required,
            )
            flash(
                f"Exposed sys.{result['procedure_name']} as {result['endpoint']} ({result['auth_required']}).",
                "info",
            )
        except Exception as exc:
            flash(f"SYS procedure exposure failed: {exc}", "error")
            return redirect(
                url_for(
                    "dashboard",
                    slug="expose-sp-as-service",
                    tab="sys",
                    service_path=service_path,
                    procedure_auth_required="Required" if auth_required else "Not Required",
                )
            )

        return redirect(url_for("dashboard", slug="list-restful-services"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            try:
                login_profile = _build_login_profile(request.form)
            except Exception as exc:
                return render_template(
                    "login.html",
                    login_error=str(exc),
                    login_username=username,
                    login_profile=active_login_profile(),
                )

            if not username or not password:
                return render_template(
                    "login.html",
                    login_error="Enter both username and password.",
                    login_username=username,
                    login_profile=login_profile,
                )

            previous_profile = session.get("connection_profile")
            if previous_profile and previous_profile != login_profile:
                stop_all_shared_tunnels()
            session["connection_profile"] = login_profile
            try:
                if login_profile.get("use_ssh_tunnel"):
                    start_shared_tunnels()
                ensure_login_tunnels()
                grants = fetch_grants(username, password)
                used_fallback = False
            except Exception as exc:
                fallback_role = fallback_user_role(username, password)
                if fallback_role is None:
                    stop_all_shared_tunnels()
                    return render_template(
                        "login.html",
                        login_error=f"Login failed: {exc}",
                        login_username=username,
                        login_profile=login_profile,
                    )
                role = fallback_role
                grants = [f"Fallback role mapping applied for {username}"]
                used_fallback = True

            if not used_fallback:
                role = classify_role(username, grants)
            session["username"] = username
            session["role"] = role
            session["initials"] = infer_initials(username)
            session["grants"] = grants
            session["tunnels_ready"] = True
            if used_fallback:
                session["login_notice"] = "DB grant lookup timed out. Applied the configured sample-user role mapping for this session."
            return redirect(url_for("dashboard", slug=role_home(role)))

        return render_template("login.html", login_profile=active_login_profile())

    @app.route("/logout", methods=["POST"])
    def logout():
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
            "config_profiles": profile_names(),
            "config_profile": get_profile(),
            "config_profile_name": get_profile()["name"],
            "update_status": read_update_status(),
            "update_poll_token": session.get("update_poll_token", ""),
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

        if user["role"] == "rest_admin":
            try:
                context.update(_dashboard_context_for_rest_admin(slug))
            except Exception as exc:
                return _dashboard_error_response(
                    slug=slug,
                    message=f"Rest Admin page failed to load: {exc}",
                    context=context,
                )

        if user["role"] in {"rest_admin", "test_user"} and slug == "list-restful-services":
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
