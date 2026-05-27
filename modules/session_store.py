from __future__ import annotations

import re
from typing import Any

from flask import session

from modules.catalog import GRANT_TABS, RESTAPIDB_TABS, ROLE_LABELS, ROLE_MENUS, SP_TABS, USER_TABS


def role_menu(role: str) -> list[dict[str, str]]:
    return ROLE_MENUS.get(role, [])


def admin_subtabs(slug: str) -> list[dict[str, str]]:
    if slug == "user":
        return USER_TABS
    if slug == "granting-privileges":
        return GRANT_TABS
    if slug == "restapidb":
        return RESTAPIDB_TABS
    if slug == "expose-sp-as-service":
        return SP_TABS
    return []


def default_subtab(slug: str) -> str:
    tabs = admin_subtabs(slug)
    return tabs[0]["slug"] if tabs else ""


def role_home(role: str) -> str:
    menu = role_menu(role)
    return menu[0]["slug"] if menu else ""


def current_user() -> dict[str, Any] | None:
    if "username" not in session:
        return None
    role = session["role"]
    return {
        "username": session["username"],
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "menu": role_menu(role),
    }


def slug_allowed(role: str, slug: str) -> bool:
    return any(item["slug"] == slug for item in role_menu(role))


def infer_initials(username: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", username) if part]
    if not parts:
        return "HW"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()
