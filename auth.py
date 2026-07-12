"""Account + session auth for Crypto Arb Autopilot."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
SESSION_COOKIE = "caa_session"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_PBKDF2_ITERS = 120_000


def _db_path() -> str:
    default = (
        "/tmp/accounts.db"
        if os.environ.get("DYNO")
        else str(Path(__file__).parent / "data" / "accounts.db")
    )
    path = Path(os.environ.get("ACCOUNTS_DB_PATH", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                password_hash TEXT,
                username TEXT UNIQUE,
                display_name TEXT NOT NULL,
                is_guest INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(users)").fetchall()}
        if "username" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
            _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            for row in _conn.execute(
                "SELECT id, email, display_name FROM users WHERE is_guest=0 AND username IS NULL"
            ):
                base = (row["email"] or row["display_name"] or row["id"]).split("@")[0].lower()
                base = re.sub(r"[^a-z0-9_-]", "", base) or row["id"][:8]
                candidate = base
                n = 1
                while _conn.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
                    candidate = f"{base}{n}"
                    n += 1
                _conn.execute("UPDATE users SET username=? WHERE id=?", (candidate, row["id"]))
        _conn.commit()
    return _conn


def _new_id() -> str:
    return secrets.token_hex(8)


def _session_expiry() -> float:
    return time.time() + SESSION_DAYS * 86400


def _normalize_username(username: str) -> str:
    username = username.strip()
    if len(username) < 2:
        raise ValueError("username must be at least 2 characters")
    if len(username) > 32:
        raise ValueError("username must be at most 32 characters")
    if not _USERNAME_RE.match(username):
        raise ValueError("username can only contain letters, numbers, underscores, and hyphens")
    return username


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt}${digest.hex()}"


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return True  # legacy username-only accounts
    try:
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


def _username_key(username: str) -> str:
    return _normalize_username(username).lower()


def _user_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    name = row["display_name"]
    if row["username"]:
        name = row["display_name"] or row["username"]
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": name,
        "is_guest": bool(row["is_guest"]),
        "created_at": row["created_at"],
    }


def _create_session(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
        (token, user_id, now, _session_expiry()),
    )
    return token


def create_guest() -> tuple[str, dict[str, Any]]:
    uid = _new_id()
    name = f"Guest {uid[:4].upper()}"
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO users(id, email, password_hash, username, display_name, is_guest, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, None, None, None, name, 1, now),
        )
        token = _create_session(conn, uid)
        conn.commit()
        user = _user_row(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    return token, user  # type: ignore[return-value]


def register(username: str, password: str, guest_id: str | None = None) -> tuple[str, dict[str, Any]]:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    display = _normalize_username(username)
    key = display.lower()
    pw_hash = _hash_password(password)
    now = time.time()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
        if row and not row["is_guest"]:
            raise ValueError("username already taken")
        if guest_id:
            guest = conn.execute("SELECT * FROM users WHERE id=? AND is_guest=1", (guest_id,)).fetchone()
            if guest:
                conn.execute(
                    "UPDATE users SET username=?, display_name=?, password_hash=?, is_guest=0 WHERE id=?",
                    (key, display, pw_hash, guest_id),
                )
                token = _create_session(conn, guest_id)
                conn.commit()
                user = _user_row(conn.execute("SELECT * FROM users WHERE id=?", (guest_id,)).fetchone())
                return token, user  # type: ignore[return-value]
        uid = _new_id()
        conn.execute(
            "INSERT INTO users(id, email, password_hash, username, display_name, is_guest, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, None, pw_hash, key, display, 0, now),
        )
        token = _create_session(conn, uid)
        conn.commit()
        user = _user_row(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    return token, user  # type: ignore[return-value]


def login(username: str, password: str = "", guest_id: str | None = None) -> tuple[str, dict[str, Any]]:
    display = _normalize_username(username)
    key = display.lower()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
        if not row or row["is_guest"]:
            raise ValueError("invalid username or password")
        if not _verify_password(password, row["password_hash"]):
            raise ValueError("invalid username or password")
        token = _create_session(conn, row["id"])
        conn.commit()
        return token, _user_row(row)  # type: ignore[return-value]


def sign_in(username: str, guest_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """Username-only sign-in (creates account if missing) — dev convenience."""
    display = _normalize_username(username)
    key = display.lower()
    now = time.time()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
        if row and not row["is_guest"]:
            token = _create_session(conn, row["id"])
            conn.commit()
            return token, _user_row(row)  # type: ignore[return-value]
        if row and row["is_guest"]:
            raise ValueError("username already taken")
        uid = _new_id()
        conn.execute(
            "INSERT INTO users(id, email, password_hash, username, display_name, is_guest, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, None, None, key, display, 0, now),
        )
        token = _create_session(conn, uid)
        conn.commit()
        user = _user_row(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    return token, user  # type: ignore[return-value]


def logout(token: str) -> None:
    with _lock:
        _connect().execute("DELETE FROM sessions WHERE token=?", (token,))
        _connect().commit()


def user_from_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND s.expires_at > ?",
            (token, time.time()),
        ).fetchone()
    return _user_row(row)


def parse_cookie(header: str | None, name: str = SESSION_COOKIE) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part.split("=", 1)[1]
    return None


def cookie_header(token: str) -> str:
    max_age = SESSION_DAYS * 86400
    secure = " Secure;" if os.environ.get("DYNO") else ""
    return (
        f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age};{secure}"
    )


def clear_cookie_header() -> str:
    secure = " Secure;" if os.environ.get("DYNO") else ""
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0;{secure}"
