from __future__ import annotations

import json
import os
from typing import Any

import mysql.connector


LOCAL_ADMIN_PROFILE_NAME = os.getenv("LOCAL_MYSQL_PROFILE_NAME", "local-admin-profile")
LOCAL_ADMIN_SOCKET = os.getenv("LOCAL_MYSQL_SOCKET", ".data/run/mysql.sock")
CONFIGDB_NAME = os.getenv("MRS_CONSOLE_CONFIGDB_NAME", "configdb")
CONFIGDB_USER = os.getenv("MRS_CONSOLE_CONFIGDB_USER", "")
CONFIGDB_PASSWORD = os.getenv("MRS_CONSOLE_CONFIGDB_PASSWORD", "")
CONFIGDB_SOCKET = os.getenv("MRS_CONSOLE_CONFIGDB_SOCKET", LOCAL_ADMIN_SOCKET)


DEFAULT_PROFILE = {
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
}

LOCAL_ADMIN_PROFILE = {
    "name": LOCAL_ADMIN_PROFILE_NAME,
    "label": "Local Admin Profile",
    "mode": "socket",
    "socket": LOCAL_ADMIN_SOCKET,
    "database": "mysql",
    "default_username": os.getenv("LOCAL_MYSQL_ADMIN_USER", "localadmin"),
    "profile_management": True,
    "force_password_change": True,
}


def _coerce_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0 or parsed > 65535:
        return default
    return parsed


def _normalize_profile(raw: dict[str, Any], fallback_name: str = "default") -> dict[str, Any]:
    if str((raw or {}).get("mode", "")).strip() == "socket":
        profile = dict(LOCAL_ADMIN_PROFILE)
        profile.update(raw or {})
        profile["name"] = str(profile.get("name") or fallback_name).strip() or fallback_name
        profile["label"] = str(profile.get("label") or profile["name"]).strip()
        profile["mode"] = "socket"
        profile["socket"] = str(profile.get("socket") or LOCAL_ADMIN_SOCKET).strip()
        profile["database"] = str(profile.get("database") or "mysql").strip()
        profile["default_username"] = str(profile.get("default_username") or "localadmin").strip()
        profile["profile_management"] = bool(profile.get("profile_management"))
        profile["force_password_change"] = bool(profile.get("force_password_change"))
        return profile

    profile = dict(DEFAULT_PROFILE)
    profile.update(raw or {})
    profile["name"] = str(profile.get("name") or fallback_name).strip() or fallback_name
    profile["label"] = str(profile.get("label") or profile["name"]).strip()
    profile["use_ssh_tunnel"] = bool(profile.get("use_ssh_tunnel"))
    profile["db_host"] = str(profile.get("db_host") or DEFAULT_PROFILE["db_host"]).strip()
    profile["api_host"] = str(profile.get("api_host") or profile["db_host"]).strip()
    profile["ssh_key_path"] = str(profile.get("ssh_key_path") or "").strip()
    profile["ssh_jump_host"] = str(profile.get("ssh_jump_host") or "").strip()
    profile["ssh_jump_user"] = str(profile.get("ssh_jump_user") or "").strip()
    profile["db_port"] = _coerce_int(profile.get("db_port"), int(DEFAULT_PROFILE["db_port"]))
    profile["api_port"] = _coerce_int(profile.get("api_port"), int(DEFAULT_PROFILE["api_port"]))
    profile["local_db_port"] = _coerce_int(profile.get("local_db_port"), int(DEFAULT_PROFILE["local_db_port"]))
    profile["local_api_port"] = _coerce_int(profile.get("local_api_port"), int(DEFAULT_PROFILE["local_api_port"]))
    return profile


def _fallback_profiles() -> list[dict[str, Any]]:
    return [_normalize_profile(LOCAL_ADMIN_PROFILE), _normalize_profile(DEFAULT_PROFILE)]


def _configdb_connection():
    if not CONFIGDB_USER or not CONFIGDB_PASSWORD:
        raise RuntimeError("configdb credentials are not configured in the runtime environment.")
    socket_path = CONFIGDB_SOCKET
    if not socket_path.startswith("/"):
        socket_path = os.path.abspath(socket_path)
    return mysql.connector.connect(
        unix_socket=socket_path,
        user=CONFIGDB_USER,
        password=CONFIGDB_PASSWORD,
        database=CONFIGDB_NAME,
        autocommit=True,
    )


def _ensure_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS connection_profiles (
                name VARCHAR(128) PRIMARY KEY,
                label VARCHAR(255) NOT NULL,
                profile_json JSON NOT NULL,
                profile_management BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )


def load_profiles() -> list[dict[str, Any]]:
    try:
        connection = _configdb_connection()
        _ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT profile_json FROM connection_profiles ORDER BY profile_management DESC, name")
            profiles = []
            for row in cursor.fetchall():
                value = row["profile_json"]
                if isinstance(value, str):
                    payload = json.loads(value)
                else:
                    payload = value
                if isinstance(payload, dict):
                    profiles.append(_normalize_profile(payload, fallback_name=str(payload.get("name", ""))))
        connection.close()
    except Exception:
        return _fallback_profiles()
    if not any(item.get("name") == LOCAL_ADMIN_PROFILE_NAME for item in profiles):
        profiles.insert(0, _normalize_profile(LOCAL_ADMIN_PROFILE))
    if not any(item.get("name") != LOCAL_ADMIN_PROFILE_NAME for item in profiles):
        profiles.append(_normalize_profile(DEFAULT_PROFILE))
    return profiles or _fallback_profiles()


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    connection = _configdb_connection()
    _ensure_schema(connection)
    with connection.cursor() as cursor:
        for profile in profiles:
            normalized = _normalize_profile(profile, fallback_name=str(profile.get("name", "default")))
            cursor.execute(
                """
                INSERT INTO connection_profiles (name, label, profile_json, profile_management)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    label = VALUES(label),
                    profile_json = VALUES(profile_json),
                    profile_management = VALUES(profile_management)
                """,
                (
                    normalized["name"],
                    normalized["label"],
                    json.dumps(normalized, sort_keys=True),
                    bool(normalized.get("profile_management")),
                ),
            )
    connection.close()


def can_manage_profiles(active_profile: dict[str, Any] | None) -> bool:
    return bool(
        active_profile
        and active_profile.get("name") == LOCAL_ADMIN_PROFILE_NAME
        and active_profile.get("mode") == "socket"
        and active_profile.get("profile_management")
    )


def profile_names() -> list[dict[str, str]]:
    return [{"name": item["name"], "label": item["label"]} for item in load_profiles()]


def get_profile(name: str | None = None) -> dict[str, Any]:
    profiles = load_profiles()
    selected = str(name or "").strip()
    for profile in profiles:
        if profile["name"] == selected:
            return dict(profile)
    return dict(profiles[0])


def update_profile(name: str, updates: dict[str, Any]) -> dict[str, Any]:
    selected = str(name or "").strip() or "default"
    profiles = load_profiles()
    for index, profile in enumerate(profiles):
        if profile["name"] == selected:
            updated = _normalize_profile({**profile, **updates, "name": selected}, fallback_name=selected)
            profiles[index] = updated
            save_profiles(profiles)
            return dict(updated)
    updated = _normalize_profile({**updates, "name": selected}, fallback_name=selected)
    profiles.append(updated)
    save_profiles(profiles)
    return dict(updated)
