from __future__ import annotations

from modules.mysql_service import (
    build_delete_rest_service_paths_definition,
    build_expose_database_to_service_definition,
    build_expose_existing_schema_procedure,
    build_rest_procedure_definition,
    build_rest_service_definition,
    build_rest_service_path_definition,
    classify_role,
    connection_dashboard_status,
    create_user_account,
    create_rest_procedure_definition,
    create_rest_service_path_definition,
    create_rest_service_definition,
    default_special_priv_category,
    delete_selected_users,
    ensure_login_tunnels,
    expose_database_to_service_definition,
    expose_existing_schema_procedure,
    fallback_user_role,
    fetch_grants,
    grant_target_users,
    grant_object_privileges,
    grant_special_privileges,
    is_system_user,
    list_databases,
    list_base_tables,
    list_mysql_admin_privileges,
    list_rest_service_paths,
    list_schema_procedures,
    list_special_privileges,
    list_table_columns,
    list_tables,
    list_users_with_roles,
    run_admin_connector_sql,
    run_admin_ddl,
    run_admin_sql,
    special_privilege_categories,
    start_shared_tunnels,
    stop_all_shared_tunnels,
)
from modules.rest_service import (
    execute_rest_endpoint,
    find_restapidb_service,
    get_rest_procedure_details,
    get_rest_service_auth_details,
    list_restapidb_objects,
    list_restapidb_services,
)


def prewarm_admin_metadata() -> None:
    for loader in (
        list_users_with_roles,
        list_databases,
        list_restapidb_objects,
        list_restapidb_services,
        list_mysql_admin_privileges,
    ):
        try:
            loader()
        except Exception:
            continue
