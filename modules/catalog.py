from __future__ import annotations


ROLE_LABELS = {
    "admin": "Admin",
    "rest_admin": "Rest Admin",
    "test_user": "Test User",
}

ROLE_MENUS = {
    "admin": [
        {"slug": "user", "label": "User"},
        {"slug": "granting-privileges", "label": "Granting Privileges"},
        {"slug": "restapidb", "label": "RestAPIDB"},
        {"slug": "config", "label": "Profiles"},
        {"slug": "update", "label": "Update"},
        {"slug": "show-grants", "label": "Show Grants"},
    ],
    "rest_admin": [
        {"slug": "list-restful-services", "label": "List Restful Services"},
        {"slug": "create-restful-service", "label": "Create Restful Service"},
        {"slug": "expose-db-as-service", "label": "Expose DB as Service"},
        {"slug": "expose-table-as-service", "label": "Expose Table as Service"},
        {"slug": "expose-sp-as-service", "label": "Expose SP as Service"},
        {"slug": "show-grants", "label": "Show Grants"},
    ],
    "test_user": [
        {"slug": "list-restful-services", "label": "List Restful Services"},
        {"slug": "show-grants", "label": "Show Grants"},
    ],
}

PAGE_CONTENT = {
    "config": {
        "title": "Profiles",
        "summary": "Define MySQL database endpoints, MySQL REST Service endpoints, and optional SSH tunnel profiles.",
        "items": [
            "Profile management is available only through local-admin-profile.",
            "Profiles contain non-secret infrastructure details only; user passwords are entered at login.",
            "The selected profile drives both DB metadata operations and REST endpoint testing.",
            "SSH private-key material stays server-side under profile_ssh_keys/ and is never rendered to the browser.",
        ],
    },
    "user": {
        "title": "User",
        "summary": "Manage application-facing MySQL and MRS users from the administrator workspace.",
        "items": [
            "Create and rotate MySQL users used by REST services.",
            "Review role classification for Admin, Rest Admin, and Test User.",
            "Keep authentication credentials out of scripts by using shared environment settings.",
        ],
    },
    "granting-privileges": {
        "title": "Granting Privileges",
        "summary": "Review the privilege model before exposing objects through MRS.",
        "items": [
            "Reserve broad grants for platform administrators only.",
            "Grant mysql_rest_service_admin only to REST administrators.",
            "Prefer minimal SELECT-only users for authenticated service consumers.",
        ],
    },
    "restapidb": {
        "title": "RestAPIDB",
        "summary": "Track the schema that stores REST-facing views and published objects.",
        "items": [
            "Published SQL views belong in restapidb.",
            "Use SQL SECURITY DEFINER views when exposing data from source schemas.",
            "Keep naming consistent between database objects and REST paths.",
        ],
    },
    "update": {
        "title": "Update",
        "summary": "Refresh the application from the configured Git repository, rerun setup, and restart active services.",
        "items": [
            "Updates require a clean worktree except for approved local runtime files.",
            "The updater verifies the configured remote and branch before pulling changes.",
            "Restricted services can run setup with SKIP_PRIVILEGED_SETUP=1 and finish privileged changes later from a shell.",
        ],
    },
    "create-restful-service": {
        "title": "Create Restful Service",
        "summary": "Prepare the service container, schema mapping, and authentication model for new APIs.",
        "items": [
            "Create the REST service path and publish it.",
            "Attach the required authentication app for protected services.",
            "Keep service comments descriptive because they surface in operational output.",
        ],
    },
    "expose-db-as-service": {
        "title": "Expose DB as Service",
        "summary": "Model database-level exposure carefully and keep the public surface narrow.",
        "items": [
            "Use a dedicated REST schema path per database domain.",
            "Separate public and authenticated services instead of mixing policy.",
            "Prefer views over direct table exposure when you need column control.",
        ],
    },
    "expose-table-as-service": {
        "title": "Expose Table as Service",
        "summary": "Publish tables or views as REST views with explicit field mapping.",
        "items": [
            "Define stable field names rather than leaking internal column names.",
            "Mark sortable identifiers explicitly when clients need pagination or ordering.",
            "Validate authentication mode on the REST schema before publishing.",
        ],
    },
    "expose-sp-as-service": {
        "title": "Expose SP as Service",
        "summary": "Stored procedures are best for controlled write flows and parameter validation.",
        "items": [
            "Keep write procedures idempotent where practical.",
            "Validate all inputs in SQL before mutating data.",
            "Expose procedures only through authenticated services.",
        ],
    },
    "test-restful-service": {
        "title": "Test Restful Service",
        "summary": "Exercise the protected endpoint with the minimal reader user and verify paging behavior.",
        "items": [
            "Log in through the service authentication endpoint and request a bearer token.",
            "Call the protected REST path with Authorization: Bearer.",
            "Verify count, hasMore, and next links for client compatibility.",
        ],
    },
    "list-restful-services": {
        "title": "List Restful Services",
        "summary": "Inspect available REST services and execute test calls directly through the local forwarded ports.",
        "items": [
            "Check whether each service has auth apps attached.",
            "Confirm whether the endpoint requires authentication before testing it.",
            "Use the Test button to execute public or authenticated REST requests.",
        ],
    },
    "show-grants": {
        "title": "Show Grants",
        "summary": "Review the grants captured for the current login so you can confirm the detected role and available privileges.",
        "items": [
            "Each row is taken from the grants returned at login time.",
            "Use this page to verify why the session was classified as Admin, Rest Admin, or Test User.",
            "Log in again if you change privileges and want the page to reflect the new grants.",
        ],
    },
}

SYSTEM_USERS = {
    "sys",
    "ociadm",
    "ocidbm",
    "ocirpl",
    "mysql_option_tracker_persister",
    "ocirest",
    "oracle-cloud-agent",
    "rrhhuser",
}

SYSTEM_USER_PREFIXES = (
    "mysql_rest_service",
    "mysql.",
)

USER_TABS = [
    {"slug": "list", "label": "List"},
    {"slug": "create", "label": "Create"},
]

GRANT_TABS = [
    {"slug": "grant-object-priv", "label": "Grant Object Priv"},
    {"slug": "special-priv", "label": "Special Priv"},
]

RESTAPIDB_TABS = [
    {"slug": "list", "label": "List"},
]

SP_TABS = [
    {"slug": "user", "label": "User"},
    {"slug": "sys", "label": "SYS"},
]

OBJECT_PRIVILEGES = ["SELECT", "UPDATE", "DELETE", "ALL"]

REST_ADMIN_ROLES = (
    "mysql_rest_service_admin",
    "mysql_rest_service_data_provider",
    "mysql_rest_service_dev",
    "mysql_rest_service_meta_provider",
    "mysql_rest_service_schema_admin",
)

SPECIAL_PRIV_CATEGORIES = [
    {"slug": "mrs-roles", "label": "HeatWave / MRS Roles"},
    {"slug": "mysql-roles", "label": "MySQL Roles"},
    {"slug": "mysql-admin", "label": "MySQL Admin Privileges"},
]

SPECIAL_ROLE_CATALOG = {
    "mrs-roles": [
        {
            "value": f"role:{role_name}",
            "name": role_name,
            "context": "HeatWave MRS role",
            "comment": "Built-in role used by HeatWave MRS administration and metadata workflows.",
        }
        for role_name in REST_ADMIN_ROLES
    ],
    "mysql-roles": [
        {
            "value": "role:administrator",
            "name": "administrator",
            "context": "MySQL role",
            "comment": "Built-in administrator role intended for trusted platform owners.",
        }
    ],
}
