from __future__ import annotations

import re
from typing import Any

from flask import session

from modules.catalog import GRANT_TABS, RESTAPIDB_TABS, ROLE_LABELS, ROLE_MENUS, SP_TABS, USER_TABS


def role_menu(role: str) -> list[dict[str, str]]:
    return ROLE_MENUS.get(role, [])


def _menu_items(menu: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in menu:
        children = entry.get("children")
        if isinstance(children, list):
            items.extend(child for child in children if isinstance(child, dict))
        else:
            items.append(entry)
    return items


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
    if role == "admin":
        return "local-users"
    if role == "local_user":
        return "profile-login"
    menu = _menu_items(role_menu(role))
    return menu[0]["slug"] if menu else ""


def current_user() -> dict[str, Any] | None:
    if "username" not in session:
        return None
    role = session["role"]
    menu = role_menu(role)
    if role == "admin" and not session.get("connection_profile"):
        menu = [item for item in menu if item.get("label") != "RestAPI"]
    return {
        "username": session["username"],
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "menu": menu,
    }


def slug_allowed(role: str, slug: str) -> bool:
    user = current_user()
    menu = user["menu"] if user else role_menu(role)
    return any(item.get("slug") == slug for item in _menu_items(menu))


def infer_initials(username: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", username) if part]
    if not parts:
        return "HW"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()
