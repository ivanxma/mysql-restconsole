from __future__ import annotations

import secrets
import time


_CREDENTIAL_TTL_SECONDS = 12 * 60 * 60
_PROFILE_CREDENTIALS: dict[str, tuple[float, str]] = {}


def store_profile_password(password: str) -> str:
    token = secrets.token_urlsafe(32)
    _PROFILE_CREDENTIALS[token] = (time.time(), password)
    return token


def get_profile_password(token: str) -> str:
    value = _PROFILE_CREDENTIALS.get(str(token or ""))
    if not value:
        return ""
    created_at, password = value
    if (time.time() - created_at) > _CREDENTIAL_TTL_SECONDS:
        _PROFILE_CREDENTIALS.pop(str(token or ""), None)
        return ""
    return password


def clear_profile_password(token: str) -> None:
    if token:
        _PROFILE_CREDENTIALS.pop(str(token), None)
