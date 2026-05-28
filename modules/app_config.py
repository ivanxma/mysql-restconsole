from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from flask import has_request_context, session

from modules.profile_store import get_profile


@dataclass(frozen=True)
class AppConfig:
    host: str = os.getenv("MRS_WEBAPP_DB_HOST", "127.0.0.1")
    port: int = int(os.getenv("MRS_WEBAPP_DB_PORT", "3306"))
    api_host: str = os.getenv("MRS_WEBAPP_API_HOST", "127.0.0.1")
    admin_username: str = os.getenv("MRS_WEBAPP_ADMIN_USER", "admin")
    admin_password: str = os.getenv("MRS_WEBAPP_ADMIN_PASSWORD", "")
    rest_admin_username: str = os.getenv("MRS_WEBAPP_REST_ADMIN_USER", "admin-rest")
    rest_admin_password: str = os.getenv("MRS_WEBAPP_REST_ADMIN_PASSWORD", "")
    test_username: str = os.getenv("MRS_WEBAPP_TEST_USER", "mrs-airline-reader")
    test_password: str = os.getenv("MRS_WEBAPP_TEST_PASSWORD", "")
    secret_key: str = os.getenv("MRS_WEBAPP_SECRET_KEY", "change-this-secret")
    ssh_key_path: str = os.getenv("MRS_WEBAPP_SSH_KEY_PATH", "")
    ssh_jump_host: str = os.getenv("MRS_WEBAPP_SSH_JUMP_HOST", "")
    ssh_jump_user: str = os.getenv("MRS_WEBAPP_SSH_JUMP_USER", "opc")
    api_port: int = int(os.getenv("MRS_WEBAPP_API_PORT", "8443"))
    connect_timeout: int = int(os.getenv("MRS_WEBAPP_DB_CONNECT_TIMEOUT", "5"))


CONFIG = AppConfig()


def default_login_profile() -> dict[str, Any]:
    return get_profile()


def active_login_profile() -> dict[str, Any]:
    profile = default_login_profile()
    if has_request_context():
        profile.update(session.get("connection_profile", {}))
    return profile


def get_runtime_config() -> AppConfig:
    if not has_request_context():
        return CONFIG

    profile = session.get("connection_profile", {})
    if not profile:
        return CONFIG

    if profile.get("mode") == "socket":
        return CONFIG

    use_ssh_tunnel = bool(profile.get("use_ssh_tunnel"))
    runtime_host = "127.0.0.1" if use_ssh_tunnel else str(profile.get("db_host", CONFIG.host))
    runtime_port = int(profile.get("local_db_port", CONFIG.port) if use_ssh_tunnel else profile.get("db_port", CONFIG.port))
    runtime_api_host = "127.0.0.1" if use_ssh_tunnel else str(profile.get("api_host", profile.get("db_host", CONFIG.api_host)))
    runtime_api_port = int(profile.get("local_api_port", CONFIG.api_port) if use_ssh_tunnel else profile.get("api_port", CONFIG.api_port))

    return AppConfig(
        host=runtime_host,
        port=runtime_port,
        api_host=runtime_api_host,
        api_port=runtime_api_port,
        admin_username=CONFIG.admin_username,
        admin_password=CONFIG.admin_password,
        rest_admin_username=CONFIG.rest_admin_username,
        rest_admin_password=CONFIG.rest_admin_password,
        test_username=CONFIG.test_username,
        test_password=CONFIG.test_password,
        secret_key=CONFIG.secret_key,
        ssh_key_path=str(profile.get("ssh_key_path", CONFIG.ssh_key_path)),
        ssh_jump_host=str(profile.get("ssh_jump_host", CONFIG.ssh_jump_host)),
        ssh_jump_user=str(profile.get("ssh_jump_user", CONFIG.ssh_jump_user)),
        connect_timeout=CONFIG.connect_timeout,
    )
