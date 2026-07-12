"""Account auth with scrypt passwords and mandatory TOTP 2FA."""

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

import pyotp

import vault

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "14"))
SESSION_COOKIE = "caa_session"
CHALLENGE_COOKIE = "caa_2fa"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SCRYPT_N = int(os.environ.get("PASSWORD_SCRYPT_N", "16384"))  # 2^14 — fits OpenSSL mem limits on macOS
_SCRYPT_R = 8
_SCRYPT_P = 1
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
                totp_secret_enc TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS auth_challenges (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_challenges_user ON auth_challenges(user_id);
        """)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(users)").fetchall()}
        if "totp_secret_enc" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN totp_secret_enc TEXT")
        if "totp_enabled" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
        # Migrate session table to hashed tokens
        sess_cols = {r[1] for r in _conn.execute("PRAGMA table_info(sessions)").fetchall()}
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
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "scrypt$" + base64_url(salt) + "$" + base64_url(digest)


def base64_url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    import base64
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        if stored.startswith("scrypt$"):
            _, salt_b64, digest_b64 = stored.split("$", 2)
            salt = _b64url_decode(salt_b64)
            expected = _b64url_decode(digest_b64)
            check = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                dklen=32,
            )
            return secrets.compare_digest(check, expected)
        # Legacy pbkdf2
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


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
        "totp_enabled": bool(row["totp_enabled"]) if "totp_enabled" in row.keys() else False,
        "created_at": row["created_at"],
    }


def _create_session(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
        (vault.hash_token(token), user_id, now, _session_expiry()),
    )
    return token


def _issue_challenge(conn: sqlite3.Connection, user_id: str, kind: str, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_challenges(token_hash, user_id, kind, expires_at) VALUES(?,?,?,?)",
        (vault.hash_token(token), user_id, kind, time.time() + ttl),
    )
    return token


def _consume_challenge(conn: sqlite3.Connection, token: str, kind: str) -> str | None:
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


def register(username: str, password: str, guest_id: str | None = None) -> dict[str, Any]:
    """Create account and return 2FA setup material (session issued after confirm)."""
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    display = _normalize_username(username)
    key = display.lower()
    pw_hash = _hash_password(password)
    totp_secret = pyotp.random_base32()
    now = time.time()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
        if row and not row["is_guest"]:
            raise ValueError("username already taken")
        uid: str | None = None
        if guest_id:
            guest = conn.execute("SELECT * FROM users WHERE id=? AND is_guest=1", (guest_id,)).fetchone()
            if guest:
                uid = guest_id
        if uid:
            secret_enc = vault.seal_string(uid, totp_secret, purpose=vault.PURPOSE_TOTP)
            conn.execute(
                "UPDATE users SET username=?, display_name=?, password_hash=?, "
                "totp_secret_enc=?, totp_enabled=0, is_guest=0 WHERE id=?",
                (key, display, pw_hash, secret_enc, uid),
            )
        else:
            uid = _new_id()
            secret_enc = vault.seal_string(uid, totp_secret, purpose=vault.PURPOSE_TOTP)
            conn.execute(
                "INSERT INTO users(id, email, password_hash, username, display_name, "
                "is_guest, totp_secret_enc, totp_enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, None, pw_hash, key, display, 0, secret_enc, 0, now),
            )
        setup_token = _issue_challenge(conn, uid, "setup", _SETUP_TTL)
        conn.commit()
    return {
        "requires_2fa_setup": True,
        "setup_token": setup_token,
        "otpauth_uri": _totp_uri(totp_secret, display),
        "totp_secret": totp_secret,
        "user": {"id": uid, "username": key, "display_name": display},
    }


def confirm_2fa_setup(setup_token: str, code: str) -> tuple[str, dict[str, Any]]:
    with _lock:
        conn = _connect()
        uid = _consume_challenge(conn, setup_token, "setup")
        if not uid:
            raise ValueError("setup expired or invalid — register again")
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row or not row["totp_secret_enc"]:
            raise ValueError("user not found")
        if not _verify_totp(uid, row["totp_secret_enc"], code):
            raise ValueError("invalid authenticator code")
        conn.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (uid,))
        token = _create_session(conn, uid)
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return token, _user_row(row)  # type: ignore[return-value]


def login(username: str, password: str) -> dict[str, Any]:
    display = _normalize_username(username)
    key = display.lower()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
        if not row or row["is_guest"]:
            raise ValueError("invalid username or password")
        if not _verify_password(password, row["password_hash"]):
            raise ValueError("invalid username or password")
        if not row["totp_enabled"]:
            if row["totp_secret_enc"]:
                setup_token = _issue_challenge(conn, row["id"], "setup", _SETUP_TTL)
                conn.commit()
                secret = vault.open_string(row["id"], row["totp_secret_enc"], purpose=vault.PURPOSE_TOTP)
                return {
                    "requires_2fa_setup": True,
                    "setup_token": setup_token,
                    "otpauth_uri": _totp_uri(secret, display),
                }
            raise ValueError("2FA not configured — contact support")
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
        if not row or not _verify_totp(uid, row["totp_secret_enc"], code):
            raise ValueError("invalid authenticator code")
        token = _create_session(conn, uid)
        conn.commit()
    return token, _user_row(row)  # type: ignore[return-value]


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
            "WHERE s.token_hash=? AND s.expires_at > ?",
            (vault.hash_token(token), time.time()),
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
