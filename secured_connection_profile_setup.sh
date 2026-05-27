#!/usr/bin/env bash
set -euo pipefail

MYSQL_BIN="${MYSQL_BIN:-.embedded/mysql-server/current/bin/mysql}"
LOCAL_PROFILE_NAME="${LOCAL_PROFILE_NAME:-local-admin-profile}"
LOCAL_MYSQL_SOCKET="${LOCAL_MYSQL_SOCKET:-.data/run/mysql.sock}"
LOCAL_MYSQL_ADMIN_USER="${LOCAL_MYSQL_ADMIN_USER:-localadmin}"
LOCAL_MYSQL_ADMIN_PASSWORD="${LOCAL_MYSQL_ADMIN_PASSWORD:-}"
LOCAL_MYSQL_DATABASE="${LOCAL_MYSQL_DATABASE:-mysql}"
CONFIGDB_NAME="${MRS_CONSOLE_CONFIGDB_NAME:-configdb}"
CONFIGDB_USER="${MRS_CONSOLE_CONFIGDB_USER:-mysql_rest_console_config}"
CONFIGDB_PASSWORD="${MRS_CONSOLE_CONFIGDB_PASSWORD:-}"

[[ -x "$MYSQL_BIN" ]] || { echo "MySQL client not found: $MYSQL_BIN" >&2; exit 1; }
[[ -n "$LOCAL_MYSQL_ADMIN_PASSWORD" ]] || { echo "LOCAL_MYSQL_ADMIN_PASSWORD is required." >&2; exit 1; }
[[ -n "$CONFIGDB_PASSWORD" ]] || { echo "MRS_CONSOLE_CONFIGDB_PASSWORD is required." >&2; exit 1; }

admin_mysql() {
  MYSQL_PWD="$LOCAL_MYSQL_ADMIN_PASSWORD" "$MYSQL_BIN" --protocol=socket --socket="$LOCAL_MYSQL_SOCKET" -u"$LOCAL_MYSQL_ADMIN_USER" "$@"
}

admin_mysql <<SQL
CREATE DATABASE IF NOT EXISTS ${CONFIGDB_NAME};
CREATE TABLE IF NOT EXISTS ${CONFIGDB_NAME}.connection_profiles (
  name VARCHAR(128) PRIMARY KEY,
  label VARCHAR(255) NOT NULL,
  profile_json JSON NOT NULL,
  profile_management BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ${CONFIGDB_NAME}.local_users (
  username VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  password_salt VARCHAR(64) NOT NULL,
  password_hash CHAR(64) NOT NULL,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  force_password_change BOOLEAN NOT NULL DEFAULT FALSE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ${CONFIGDB_NAME}.local_groups (
  group_name VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ${CONFIGDB_NAME}.local_user_groups (
  username VARCHAR(128) NOT NULL,
  group_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (username, group_name)
);
CREATE TABLE IF NOT EXISTS ${CONFIGDB_NAME}.profile_assignments (
  profile_name VARCHAR(128) NOT NULL,
  subject_type ENUM('user','group') NOT NULL,
  subject_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (profile_name, subject_type, subject_name)
);
INSERT INTO ${CONFIGDB_NAME}.local_groups (group_name, display_name, is_admin)
VALUES ('Admin', 'Admin', TRUE), ('General User', 'General User', FALSE)
ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), is_admin=VALUES(is_admin);
INSERT INTO ${CONFIGDB_NAME}.local_users (username, display_name, password_salt, password_hash, is_admin, force_password_change)
VALUES ('localadmin', 'Local Admin', 'bootstrap', SHA2('bootstrap:localadmin', 256), TRUE, TRUE)
ON DUPLICATE KEY UPDATE username=username;
INSERT IGNORE INTO ${CONFIGDB_NAME}.local_user_groups (username, group_name)
VALUES ('localadmin', 'Admin');
CREATE USER IF NOT EXISTS '${CONFIGDB_USER}'@'localhost' IDENTIFIED BY '${CONFIGDB_PASSWORD}';
ALTER USER '${CONFIGDB_USER}'@'localhost' IDENTIFIED BY '${CONFIGDB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${CONFIGDB_NAME}.* TO '${CONFIGDB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

python3 - "$MYSQL_BIN" "$LOCAL_MYSQL_SOCKET" "$LOCAL_MYSQL_ADMIN_USER" "$LOCAL_MYSQL_ADMIN_PASSWORD" "$CONFIGDB_NAME" "$LOCAL_PROFILE_NAME" "$LOCAL_MYSQL_SOCKET" "$LOCAL_MYSQL_ADMIN_USER" "$LOCAL_MYSQL_DATABASE" <<'PY'
import json
import os
import subprocess
import sys

mysql_bin, socket_path, admin_user, admin_password, db_name, profile_name, profile_socket, default_user, database = sys.argv[1:]
profiles = [
{
    "name": profile_name,
    "label": "Local Admin Profile",
    "mode": "socket",
    "socket": profile_socket,
    "database": database,
    "default_username": default_user,
    "profile_management": True,
    "force_password_change": True,
},
{
    "name": "default",
    "label": "Local MySQL REST Service",
    "use_ssh_tunnel": False,
    "db_host": os.getenv("MRS_REMOTE_DB_HOST", "127.0.0.1"),
    "db_port": int(os.getenv("MRS_REMOTE_DB_PORT", "3306")),
    "api_host": os.getenv("MRS_REMOTE_API_HOST", os.getenv("MRS_REMOTE_DB_HOST", "127.0.0.1")),
    "api_port": int(os.getenv("MRS_REMOTE_API_PORT", "443")),
    "local_db_port": int(os.getenv("MRS_LOCAL_DB_PORT", "3306")),
    "local_api_port": int(os.getenv("MRS_LOCAL_API_PORT", "8443")),
    "ssh_key_path": os.getenv("MRS_WEBAPP_SSH_KEY_PATH", ""),
    "ssh_jump_host": os.getenv("MRS_WEBAPP_SSH_JUMP_HOST", ""),
    "ssh_jump_user": os.getenv("MRS_WEBAPP_SSH_JUMP_USER", "opc"),
},
]
sql = """
INSERT INTO connection_profiles (name, label, profile_json, profile_management)
VALUES (%s, %s, %s, %s)
ON DUPLICATE KEY UPDATE label = VALUES(label), profile_json = VALUES(profile_json), profile_management = VALUES(profile_management)
"""
env = os.environ.copy()
env["MYSQL_PWD"] = admin_password
for profile in profiles:
    payload = json.dumps(profile, sort_keys=True)
    management = "TRUE" if profile.get("profile_management") else "FALSE"
    escaped = [str(value).replace("\\", "\\\\").replace("'", "\\'") for value in (profile["name"], profile["label"], payload)]
    statement = sql.replace("%s", "'{}'", 3).replace("%s", "{}", 1).format(*escaped, management)
    subprocess.run(
        [mysql_bin, "--protocol=socket", f"--socket={socket_path}", f"-u{admin_user}", db_name, "-e", statement],
        env=env,
        check=True,
    )
PY
