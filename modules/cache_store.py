from __future__ import annotations

import time
from typing import Any


RESTAPIDB_CACHE_TTL_SECONDS = 120
RESTAPIDB_CACHE: dict[str, tuple[float, Any]] = {}


def get_cached_value(cache_key: str) -> Any | None:
    cached = RESTAPIDB_CACHE.get(cache_key)
    if not cached:
        return None
    cached_at, value = cached
    if (time.time() - cached_at) > RESTAPIDB_CACHE_TTL_SECONDS:
        RESTAPIDB_CACHE.pop(cache_key, None)
        return None
    return value


def set_cached_value(cache_key: str, value: Any) -> Any:
    RESTAPIDB_CACHE[cache_key] = (time.time(), value)
    return value


def invalidate_cached_values(*prefixes: str) -> None:
    if not prefixes:
        RESTAPIDB_CACHE.clear()
        return
    for cache_key in list(RESTAPIDB_CACHE.keys()):
        if any(cache_key.startswith(prefix) for prefix in prefixes):
            RESTAPIDB_CACHE.pop(cache_key, None)
