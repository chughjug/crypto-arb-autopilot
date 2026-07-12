"""Account auth with mandatory TOTP 2FA (username only — no passwords)."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import pyotp

import db
import vault

_lock = threading.Lock()
_conn = None

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "14"))
SESSION_COOKIE = "caa_session"
CHALLENGE_COOKIE = "caa_2fa"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_CHALLENGE_TTL = int(os.environ.get("AUTH_CHALLENGE_TTL", "300"))
_SETUP_TTL = int(os.environ.get("AUTH_SETUP_TTL", "900"))
_TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "Crypto Arb")


def _db_path() -> str:
    default = (
        "/tmp/accounts.db"
        if os.environ.get("DYNO")
        else str(Path(__file__).parent / "data" / "accounts.db")
    )
    path = Path(os.environ.get("ACCOUNTS_DB_PATH", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect():
    global _conn
    if _conn is None:
        _conn = db.connect(sqlite_path=_db_path())
        if not db.use_postgres():
            _conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    username TEXT UNIQUE,
                    display_name TEXT NOT NULL,
                    is_guest INTEGER NOT NULL DEFAULT 0,
                    totp_secret_enc TEXT,
                    totp_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_challenges (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_challenges_user ON auth_challenges(user_id);
            """)
            cols = {r["name"] for r in _conn.execute("PRAGMA table_info(users)").fetchall()}
            if "totp_secret_enc" not in cols:
                _conn.execute("ALTER TABLE users ADD COLUMN totp_secret_enc TEXT")
            if "totp_enabled" not in cols:
                _conn.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
            sess_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "token" in sess_cols and "token_hash" not in sess_cols:
                _conn.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
                _conn.executescript("""
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """)
            ch_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(auth_challenges)").fetchall()}
            if "payload" not in ch_cols:
                _conn.execute("ALTER TABLE auth_challenges ADD COLUMN payload TEXT")
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


def _user_row(row) -> dict[str, Any] | None:
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
        "totp_enabled": bool(row["totp_enabled"]) if "totp_enabled" in row.keys() else False,
        "created_at": row["created_at"],
    }


def _create_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
        (vault.hash_token(token), user_id, now, _session_expiry()),
    )
    return token


def _issue_challenge(
    conn,
    user_id: str,
    kind: str,
    ttl: int,
    payload: str | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_challenges(token_hash, user_id, kind, expires_at, payload) VALUES(?,?,?,?,?)",
        (vault.hash_token(token), user_id, kind, time.time() + ttl, payload),
    )
    return token


def _consume_challenge(conn, token: str, kind: str) -> str | None:
    th = vault.hash_token(token)
    row = conn.execute(
        "SELECT user_id, expires_at FROM auth_challenges WHERE token_hash=? AND kind=?",
        (th, kind),
    ).fetchone()
    if not row or row["expires_at"] < time.time():
        conn.execute("DELETE FROM auth_challenges WHERE token_hash=?", (th,))
        return None
    conn.execute("DELETE FROM auth_challenges WHERE token_hash=?", (th,))
    return row["user_id"]


def _username_taken(conn, key: str) -> bool:
    if conn.execute("SELECT 1 AS n FROM users WHERE username=?", (key,)).fetchone():
        return True
    now = time.time()
    rows = conn.execute(
        "SELECT payload FROM auth_challenges WHERE kind='setup' AND expires_at > ?",
        (now,),
    ).fetchall()
    for row in rows:
        if not row["payload"]:
            continue
        try:
            data = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        if data.get("username") == key:
            return True
    return False


def _consume_setup_challenge(conn, token: str) -> dict[str, Any] | None:
    th = vault.hash_token(token)
    row = conn.execute(
        "SELECT user_id, expires_at, payload FROM auth_challenges WHERE token_hash=? AND kind='setup'",
        (th,),
    ).fetchone()
    if not row or row["expires_at"] < time.time() or not row["payload"]:
        conn.execute("DELETE FROM auth_challenges WHERE token_hash=?", (th,))
        return None
    conn.execute("DELETE FROM auth_challenges WHERE token_hash=?", (th,))
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    data["pending_id"] = row["user_id"]
    return data


def _totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=_TOTP_ISSUER)


def _verify_totp(user_id: str, secret_enc: str, code: str) -> bool:
    if not secret_enc or not code:
        return False
    try:
        secret = vault.open_string(user_id, secret_enc, purpose=vault.PURPOSE_TOTP)
        totp = pyotp.TOTP(secret)
        return totp.verify(str(code).strip().replace(" ", ""), valid_window=1)
    except Exception:
        return False


def register(username: str) -> dict[str, Any]:
    """Reserve username and return 2FA setup material. User row created only after confirm."""
    display = _normalize_username(username)
    key = display.lower()
    totp_secret = pyotp.random_base32()
    pending_id = _new_id()
    secret_enc = vault.seal_string(pending_id, totp_secret, purpose=vault.PURPOSE_TOTP)
    payload = json.dumps({
        "username": key,
        "display_name": display,
        "totp_secret_enc": secret_enc,
    })
    with _lock:
        conn = _connect()
        if _username_taken(conn, key):
            raise ValueError("username already taken")
        setup_token = _issue_challenge(conn, pending_id, "setup", _SETUP_TTL, payload)
        conn.commit()
    return {
        "requires_2fa_setup": True,
        "setup_token": setup_token,
        "otpauth_uri": _totp_uri(totp_secret, display),
        "totp_secret": totp_secret,
        "user": {"username": key, "display_name": display},
    }


def confirm_2fa_setup(setup_token: str, code: str) -> tuple[str, dict[str, Any]]:
    with _lock:
        conn = _connect()
        pending = _consume_setup_challenge(conn, setup_token)
        if not pending:
            raise ValueError("setup expired or invalid — register again")
        pending_id = pending["pending_id"]
        key = pending["username"]
        display = pending["display_name"]
        secret_enc = pending["totp_secret_enc"]
        if not _verify_totp(pending_id, secret_enc, code):
            raise ValueError("invalid authenticator code")
        if conn.execute("SELECT 1 AS n FROM users WHERE username=?", (key,)).fetchone():
            raise ValueError("username already taken")
        uid = _new_id()
        now = time.time()
        secret = vault.open_string(pending_id, secret_enc, purpose=vault.PURPOSE_TOTP)
        final_enc = vault.seal_string(uid, secret, purpose=vault.PURPOSE_TOTP)
        conn.execute(
            "INSERT INTO users(id, email, password_hash, username, display_name, "
            "is_guest, totp_secret_enc, totp_enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, None, None, key, display, False, final_enc, True, now),
        )
        token = _create_session(conn, uid)
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return token, _user_row(row)  # type: ignore[return-value]


def login(username: str) -> dict[str, Any]:
    display = _normalize_username(username)
    key = display.lower()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND totp_enabled=?",
            (key, True),
        ).fetchone()
        if not row or row["is_guest"]:
            raise ValueError("unknown username — register first")
        challenge = _issue_challenge(conn, row["id"], "login", _CHALLENGE_TTL)
        conn.commit()
    return {"requires_2fa": True, "challenge_token": challenge}


def verify_2fa(challenge_token: str, code: str) -> tuple[str, dict[str, Any]]:
    with _lock:
        conn = _connect()
        uid = _consume_challenge(conn, challenge_token, "login")
        if not uid:
            raise ValueError("login expired — sign in again")
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row or not row["totp_enabled"] or not _verify_totp(uid, row["totp_secret_enc"], code):
            raise ValueError("invalid authenticator code")
        token = _create_session(conn, uid)
        conn.commit()
    return token, _user_row(row)  # type: ignore[return-value]


def logout(token: str) -> None:
    with _lock:
        _connect().execute("DELETE FROM sessions WHERE token_hash=?", (vault.hash_token(token),))
        _connect().commit()


def user_from_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > ? AND u.totp_enabled=? AND u.is_guest=?",
            (vault.hash_token(token), time.time(), True, False),
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
        f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age};{secure}"
    )


def clear_cookie_header() -> str:
    secure = " Secure;" if os.environ.get("DYNO") else ""
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0;{secure}"
