from __future__ import annotations

import time
from typing import Any

from flask import has_request_context, session


RESTAPIDB_CACHE_TTL_SECONDS = 120
RESTAPIDB_CACHE: dict[str, tuple[float, Any]] = {}


def scoped_cache_key(cache_key: str) -> str:
    if not has_request_context():
        return cache_key
    profile = session.get("connection_profile") or {}
    if not profile:
        return cache_key
    profile_name = str(profile.get("name", "profile"))
    db_username = str(session.get("db_username", ""))
    return f"profile:{profile_name}:{db_username}:{cache_key}"


def get_cached_value(cache_key: str) -> Any | None:
    cache_key = scoped_cache_key(cache_key)
    cached = RESTAPIDB_CACHE.get(cache_key)
    if not cached:
        return None
    cached_at, value = cached
    if (time.time() - cached_at) > RESTAPIDB_CACHE_TTL_SECONDS:
        RESTAPIDB_CACHE.pop(cache_key, None)
        return None
    return value


def set_cached_value(cache_key: str, value: Any) -> Any:
    cache_key = scoped_cache_key(cache_key)
    RESTAPIDB_CACHE[cache_key] = (time.time(), value)
    return value


def invalidate_cached_values(*prefixes: str) -> None:
    if not prefixes:
        RESTAPIDB_CACHE.clear()
        return
    for cache_key in list(RESTAPIDB_CACHE.keys()):
        if any(cache_key.startswith(prefix) or f":{prefix}" in cache_key for prefix in prefixes):
            RESTAPIDB_CACHE.pop(cache_key, None)
