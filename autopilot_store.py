"""Per-user autopilot config and encrypted venue credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

VENUES = ("kalshi", "polymarket", "cryptocom")


def _db_path() -> str:
    default = (
        "/tmp/autopilot.db"
        if os.environ.get("DYNO")
        else str(Path(__file__).parent / "data" / "autopilot.db")
    )
    path = Path(os.environ.get("AUTOPILOT_DB_PATH", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _fernet() -> Fernet:
    raw = os.environ.get("AUTOPILOT_SECRET_KEY", "").strip()
    if not raw:
        raw = os.environ.get("SESSION_SECRET", "dev-autopilot-secret-change-me")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS venue_credentials (
                user_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                enc_payload TEXT NOT NULL,
                connected_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, venue)
            );
            CREATE TABLE IF NOT EXISTS autopilot_config (
                user_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL DEFAULT 'half_kelly',
                bankroll_usd REAL NOT NULL DEFAULT 300,
                live_mode INTEGER NOT NULL DEFAULT 0,
                max_exposure_pct REAL,
                reserve_pct REAL NOT NULL DEFAULT 30,
                running INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS autopilot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ts REAL NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_autopilot_log_user ON autopilot_log(user_id, ts DESC);
        """)
        _conn.commit()
    return _conn


def _encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def _decrypt(blob: str) -> dict:
    return json.loads(_fernet().decrypt(blob.encode()).decode())


def save_venue_credentials(user_id: str, venue: str, payload: dict) -> dict:
    if venue not in VENUES:
        raise ValueError(f"Unknown venue: {venue}")
    now = time.time()
    enc = _encrypt(payload)
    with _lock:
        _connect().execute(
            """INSERT INTO venue_credentials(user_id, venue, enc_payload, connected_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, venue) DO UPDATE SET
                 enc_payload=excluded.enc_payload,
                 updated_at=excluded.updated_at""",
            (user_id, venue, enc, now, now),
        )
        _connect().commit()
    return venue_status(user_id, venue)


def delete_venue_credentials(user_id: str, venue: str) -> None:
    with _lock:
        _connect().execute(
            "DELETE FROM venue_credentials WHERE user_id=? AND venue=?",
            (user_id, venue),
        )
        _connect().commit()


def get_venue_credentials(user_id: str, venue: str) -> dict | None:
    with _lock:
        row = _connect().execute(
            "SELECT enc_payload FROM venue_credentials WHERE user_id=? AND venue=?",
            (user_id, venue),
        ).fetchone()
    if not row:
        return None
    return _decrypt(row["enc_payload"])


def venue_status(user_id: str, venue: str) -> dict:
    creds = get_venue_credentials(user_id, venue)
    if not creds:
        return {"venue": venue, "connected": False}
    masked = {"venue": venue, "connected": True, "updated_at": creds.get("_updated_at")}
    if venue == "kalshi":
        masked["api_key"] = _mask(creds.get("api_key", ""))
        masked["demo"] = bool(creds.get("demo", True))
    elif venue == "polymarket":
        masked["funder"] = _mask(creds.get("funder", ""))
        masked["has_private_key"] = bool(creds.get("private_key"))
    elif venue == "cryptocom":
        masked["api_key"] = _mask(creds.get("api_key", ""))
        masked["has_secret"] = bool(creds.get("api_secret"))
    return masked


def _mask(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "••••" if value else ""
    return f"{value[:4]}…{value[-4:]}"


def all_venue_status(user_id: str) -> list[dict]:
    return [venue_status(user_id, v) for v in VENUES]


def get_config(user_id: str) -> dict:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM autopilot_config WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return default_config(user_id)
    return _config_row(row)


def default_config(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "strategy_id": "half_kelly",
        "bankroll_usd": 300.0,
        "live_mode": False,
        "max_exposure_pct": None,
        "reserve_pct": 30.0,
        "running": False,
    }


def save_config(user_id: str, updates: dict) -> dict:
    current = get_config(user_id)
    current.update({k: v for k, v in updates.items() if k in (
        "strategy_id", "bankroll_usd", "live_mode", "max_exposure_pct", "reserve_pct", "running",
    )})
    now = time.time()
    with _lock:
        _connect().execute(
            """INSERT INTO autopilot_config
               (user_id, strategy_id, bankroll_usd, live_mode, max_exposure_pct,
                reserve_pct, running, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 strategy_id=excluded.strategy_id,
                 bankroll_usd=excluded.bankroll_usd,
                 live_mode=excluded.live_mode,
                 max_exposure_pct=excluded.max_exposure_pct,
                 reserve_pct=excluded.reserve_pct,
                 running=excluded.running,
                 updated_at=excluded.updated_at""",
            (
                user_id,
                current["strategy_id"],
                float(current["bankroll_usd"]),
                1 if current.get("live_mode") else 0,
                current.get("max_exposure_pct"),
                float(current.get("reserve_pct", 30)),
                1 if current.get("running") else 0,
                now,
                now,
            ),
        )
        _connect().commit()
    return get_config(user_id)


def _config_row(row: sqlite3.Row) -> dict:
    return {
        "user_id": row["user_id"],
        "strategy_id": row["strategy_id"],
        "bankroll_usd": float(row["bankroll_usd"]),
        "live_mode": bool(row["live_mode"]),
        "max_exposure_pct": row["max_exposure_pct"],
        "reserve_pct": float(row["reserve_pct"]),
        "running": bool(row["running"]),
        "updated_at": row["updated_at"],
    }


def append_log(user_id: str, level: str, message: str, detail: Any = None) -> None:
    with _lock:
        _connect().execute(
            "INSERT INTO autopilot_log(user_id, ts, level, message, detail) VALUES (?, ?, ?, ?, ?)",
            (user_id, time.time(), level, message, json.dumps(detail) if detail is not None else None),
        )
        _connect().commit()


def recent_logs(user_id: str, limit: int = 50) -> list[dict]:
    with _lock:
        rows = _connect().execute(
            "SELECT ts, level, message, detail FROM autopilot_log WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = {
            "ts": row["ts"],
            "level": row["level"],
            "message": row["message"],
        }
        if row["detail"]:
            try:
                item["detail"] = json.loads(row["detail"])
            except json.JSONDecodeError:
                item["detail"] = row["detail"]
        out.append(item)
    return out
