from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROFILE_FILE = Path(os.getenv("MRS_CONSOLE_PROFILE_FILE", "profiles.json"))


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


def _coerce_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0 or parsed > 65535:
        return default
    return parsed


def _normalize_profile(raw: dict[str, Any], fallback_name: str = "default") -> dict[str, Any]:
    profile = dict(DEFAULT_PROFILE)
    profile.update(raw or {})
    name = str(profile.get("name") or fallback_name).strip() or fallback_name
    profile["name"] = name
    profile["label"] = str(profile.get("label") or name).strip()
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


def load_profiles() -> list[dict[str, Any]]:
    if not PROFILE_FILE.exists():
        return [_normalize_profile(DEFAULT_PROFILE)]
    try:
        payload = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [_normalize_profile(DEFAULT_PROFILE)]
    raw_profiles = payload.get("profiles", []) if isinstance(payload, dict) else []
    profiles = [
        _normalize_profile(item, fallback_name=f"profile-{index}")
        for index, item in enumerate(raw_profiles, start=1)
        if isinstance(item, dict)
    ]
    return profiles or [_normalize_profile(DEFAULT_PROFILE)]


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    normalized = [_normalize_profile(item, fallback_name=f"profile-{index}") for index, item in enumerate(profiles, start=1)]
    PROFILE_FILE.write_text(json.dumps({"profiles": normalized}, indent=2) + "\n", encoding="utf-8")
    try:
        PROFILE_FILE.chmod(0o600)
    except OSError:
        pass


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
    found = False
    updated_profile: dict[str, Any] | None = None
    for index, profile in enumerate(profiles):
        if profile["name"] == selected:
            updated_profile = _normalize_profile({**profile, **updates, "name": selected}, fallback_name=selected)
            profiles[index] = updated_profile
            found = True
            break
    if not found:
        updated_profile = _normalize_profile({**updates, "name": selected}, fallback_name=selected)
        profiles.append(updated_profile)
    save_profiles(profiles)
    return dict(updated_profile)
