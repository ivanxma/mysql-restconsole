#!/usr/bin/env bash
set -euo pipefail

PROFILE_STORE="${PROFILE_STORE:-profiles.json}"
SSH_KEY_DIR="${SSH_KEY_DIR:-profile_ssh_keys}"
LOCAL_PROFILE_NAME="${LOCAL_PROFILE_NAME:-local-admin-profile}"
LOCAL_MYSQL_SOCKET="${LOCAL_MYSQL_SOCKET:-.data/run/mysql.sock}"
LOCAL_MYSQL_ADMIN_USER="${LOCAL_MYSQL_ADMIN_USER:-localadmin}"
LOCAL_MYSQL_DATABASE="${LOCAL_MYSQL_DATABASE:-mysql}"
FORCE_PASSWORD_CHANGE="${FORCE_PASSWORD_CHANGE:-1}"

mkdir -p "$(dirname "$PROFILE_STORE")" "$SSH_KEY_DIR"
chmod 0700 "$SSH_KEY_DIR"

python3 - "$PROFILE_STORE" "$LOCAL_PROFILE_NAME" "$LOCAL_MYSQL_SOCKET" "$LOCAL_MYSQL_ADMIN_USER" "$LOCAL_MYSQL_DATABASE" "$FORCE_PASSWORD_CHANGE" <<'PY'
import json
import os
import sys
from pathlib import Path

profile_store = Path(sys.argv[1])
profile_name = sys.argv[2]
socket_path = sys.argv[3]
admin_user = sys.argv[4]
database = sys.argv[5]
force_password_change = sys.argv[6] not in {"0", "false", "False", "no", "No"}

payload = {}
if profile_store.exists():
    payload = json.loads(profile_store.read_text(encoding="utf-8"))

profiles = {}
for item in payload.get("profiles", []) if isinstance(payload, dict) else []:
    if isinstance(item, dict) and item.get("name"):
        clean = {k: v for k, v in item.items() if k.lower() not in {"password", "secret", "token", "dsn", "private_key", "private_key_passphrase"}}
        clean["profile_management"] = False
        profiles[str(clean["name"])] = clean

profiles[profile_name] = {
    "name": profile_name,
    "label": "Local Admin Profile",
    "mode": "socket",
    "socket": socket_path,
    "database": database,
    "default_username": admin_user,
    "profile_management": True,
    "force_password_change": force_password_change,
}

profile_store.parent.mkdir(parents=True, exist_ok=True)
tmp = profile_store.with_suffix(profile_store.suffix + ".tmp")
tmp.write_text(json.dumps({"profiles": list(profiles.values())}, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
tmp.replace(profile_store)
os.chmod(profile_store, 0o600)
PY
