from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any

import mysql.connector


CONFIGDB_NAME = os.getenv("MRS_CONSOLE_CONFIGDB_NAME", "configdb")
CONFIGDB_USER = os.getenv("MRS_CONSOLE_CONFIGDB_USER", "")
CONFIGDB_PASSWORD = os.getenv("MRS_CONSOLE_CONFIGDB_PASSWORD", "")
CONFIGDB_SOCKET = os.getenv("MRS_CONSOLE_CONFIGDB_SOCKET", os.getenv("LOCAL_MYSQL_SOCKET", ".data/run/mysql.sock"))


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def _connection():
    if not CONFIGDB_USER or not CONFIGDB_PASSWORD:
        raise RuntimeError("configdb credentials are not configured.")
    socket_path = CONFIGDB_SOCKET if CONFIGDB_SOCKET.startswith("/") else os.path.abspath(CONFIGDB_SOCKET)
    return mysql.connector.connect(
        unix_socket=socket_path,
        user=CONFIGDB_USER,
        password=CONFIGDB_PASSWORD,
        database=CONFIGDB_NAME,
        autocommit=True,
    )


def ensure_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS local_users (
                username VARCHAR(128) PRIMARY KEY,
                display_name VARCHAR(255) NOT NULL,
                password_salt VARCHAR(64) NOT NULL,
                password_hash CHAR(64) NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                force_password_change BOOLEAN NOT NULL DEFAULT FALSE,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS local_groups (
                group_name VARCHAR(128) PRIMARY KEY,
                display_name VARCHAR(255) NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS local_user_groups (
                username VARCHAR(128) NOT NULL,
                group_name VARCHAR(128) NOT NULL,
                PRIMARY KEY (username, group_name)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_assignments (
                profile_name VARCHAR(128) NOT NULL,
                subject_type ENUM('user','group') NOT NULL,
                subject_name VARCHAR(128) NOT NULL,
                PRIMARY KEY (profile_name, subject_type, subject_name)
            )
            """
        )


def authenticate_local_user(username: str, password: str) -> dict[str, Any] | None:
    if username == "localadmin" and password == "localadmin":
        fallback = {
            "username": "localadmin",
            "display_name": "Local Admin",
            "is_admin": True,
            "force_password_change": True,
        }
    else:
        fallback = None
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT u.*,
                       COALESCE(MAX(g.is_admin), 0) AS group_is_admin
                FROM local_users u
                LEFT JOIN local_user_groups ug ON ug.username = u.username
                LEFT JOIN local_groups g ON g.group_name = ug.group_name
                WHERE u.username = %s AND u.active = TRUE
                GROUP BY u.username, u.display_name, u.password_salt, u.password_hash,
                         u.is_admin, u.force_password_change, u.active, u.created_at, u.updated_at
                """,
                (username,),
            )
            row = cursor.fetchone()
        connection.close()
    except Exception:
        return fallback
    if not row:
        return None
    expected = _hash_password(password, str(row["password_salt"]))
    if not secrets.compare_digest(expected, str(row["password_hash"])):
        return None
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]) or bool(row.get("group_is_admin")),
        "force_password_change": bool(row["force_password_change"]),
    }


def change_local_password(username: str, password: str) -> None:
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    connection = _connection()
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE local_users SET password_salt=%s, password_hash=%s, force_password_change=FALSE WHERE username=%s",
            (salt, password_hash, username),
        )
    connection.close()


def create_local_user(username: str, password: str, *, is_admin: bool = False, display_name: str = "") -> None:
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    connection = _connection()
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO local_groups (group_name, display_name, is_admin)
            VALUES ('Admin', 'Admin', TRUE), ('General User', 'General User', FALSE)
            ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), is_admin=VALUES(is_admin)
            """
        )
        cursor.execute(
            """
            INSERT INTO local_users (username, display_name, password_salt, password_hash, is_admin, force_password_change)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), is_admin=VALUES(is_admin), active=TRUE
            """,
            (username, display_name or username, salt, password_hash, is_admin),
        )
        cursor.execute(
            """
            INSERT IGNORE INTO local_user_groups (username, group_name)
            VALUES (%s, %s)
            """,
            (username, "Admin" if is_admin else "General User"),
        )
    connection.close()


def list_local_users() -> list[dict[str, Any]]:
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT username, display_name, is_admin, force_password_change, active FROM local_users ORDER BY username")
            rows = cursor.fetchall()
        connection.close()
        return [dict(row) for row in rows]
    except Exception:
        return [{"username": "localadmin", "display_name": "Local Admin", "is_admin": True, "force_password_change": True, "active": True}]


def create_local_group(group_name: str, *, is_admin: bool = False, display_name: str = "") -> None:
    connection = _connection()
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO local_groups (group_name, display_name, is_admin)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), is_admin=VALUES(is_admin)
            """,
            (group_name, display_name or group_name, is_admin),
        )
    connection.close()


def list_local_groups() -> list[dict[str, Any]]:
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT group_name, display_name, is_admin FROM local_groups ORDER BY group_name")
            rows = cursor.fetchall()
        connection.close()
        return [dict(row) for row in rows]
    except Exception:
        return [{"group_name": "Admin", "display_name": "Admin", "is_admin": True}, {"group_name": "General User", "display_name": "General User", "is_admin": False}]


def add_user_to_group(username: str, group_name: str) -> None:
    connection = _connection()
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT username FROM local_users WHERE username = %s AND active = TRUE", (username,))
        if cursor.fetchone() is None:
            raise RuntimeError("Local user does not exist.")
        cursor.execute("SELECT group_name FROM local_groups WHERE group_name = %s", (group_name,))
        if cursor.fetchone() is None:
            raise RuntimeError("Local group does not exist.")
        cursor.execute(
            """
            INSERT IGNORE INTO local_user_groups (username, group_name)
            VALUES (%s, %s)
            """,
            (username, group_name),
        )
    connection.close()


def list_user_group_memberships() -> list[dict[str, Any]]:
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT ug.username, ug.group_name, g.display_name, g.is_admin
                FROM local_user_groups ug
                LEFT JOIN local_groups g ON g.group_name = ug.group_name
                ORDER BY ug.group_name, ug.username
                """
            )
            rows = cursor.fetchall()
        connection.close()
        return [dict(row) for row in rows]
    except Exception:
        return [{"username": "localadmin", "group_name": "Admin", "display_name": "Admin", "is_admin": True}]


def assigned_profile_names(username: str) -> set[str]:
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT pa.profile_name
                FROM profile_assignments pa
                LEFT JOIN local_user_groups lug
                  ON pa.subject_type='group' AND pa.subject_name=lug.group_name
                WHERE (pa.subject_type='user' AND pa.subject_name=%s)
                   OR (lug.username=%s)
                """,
                (username, username),
            )
            rows = cursor.fetchall()
        connection.close()
        return {str(row[0]) for row in rows}
    except Exception:
        return set()


def assign_profile(profile_name: str, subject_type: str, subject_name: str) -> None:
    if subject_type not in {"user", "group"}:
        raise RuntimeError("Assignment target must be user or group.")
    connection = _connection()
    ensure_schema(connection)
    with connection.cursor() as cursor:
        if subject_type == "user":
            cursor.execute("SELECT username FROM local_users WHERE username = %s AND active = TRUE", (subject_name,))
        else:
            cursor.execute("SELECT group_name FROM local_groups WHERE group_name = %s", (subject_name,))
        if cursor.fetchone() is None:
            raise RuntimeError(f"Assignment target {subject_name} does not exist.")
        cursor.execute(
            """
            INSERT INTO profile_assignments (profile_name, subject_type, subject_name)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE profile_name=VALUES(profile_name)
            """,
            (profile_name, subject_type, subject_name),
        )
    connection.close()


def list_profile_assignments() -> list[dict[str, Any]]:
    try:
        connection = _connection()
        ensure_schema(connection)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT profile_name, subject_type, subject_name FROM profile_assignments ORDER BY profile_name, subject_type, subject_name")
            rows = cursor.fetchall()
        connection.close()
        return [dict(row) for row in rows]
    except Exception:
        return []
