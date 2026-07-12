"""Per-user autopilot config, encrypted venue credentials, logs, and trade history."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import db
from vault import credential_fingerprint, open_sensitive_payload, redact_secrets, seal_sensitive_payload

_lock = threading.Lock()
_conn = None

VENUES = ("kalshi", "polymarket", "cryptocom")

# Strategy parameter overrides users may tune per-run, with clamp bounds.
# Values are stored in engine-native units (dollars / fractions / counts).
OVERRIDE_LIMITS: dict[str, tuple[float, float]] = {
    "min_edge": (0.0, 0.50),          # dollars of net edge per contract pair
    "max_strike_gap": (0.0, 0.05),    # fraction of strike price
    "max_bet_pct": (0.005, 1.0),      # fraction of bankroll per trade
    "max_exposure_pct": (0.05, 1.0),  # fraction of bankroll deployed
    "kelly_frac": (0.05, 1.5),        # Kelly multiplier
    "flat_contracts": (1, 500),       # contracts per trade (flat_unit)
}
_INT_OVERRIDES = ("flat_contracts",)


def _clean_overrides(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float | int] = {}
    for key, (lo, hi) in OVERRIDE_LIMITS.items():
        val = raw.get(key)
        if val is None or val == "":
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        num = min(hi, max(lo, num))
        out[key] = int(num) if key in _INT_OVERRIDES else num
    return out


def _db_path() -> str:
    default = (
        "/tmp/autopilot.db"
        if os.environ.get("DYNO")
        else str(Path(__file__).parent / "data" / "autopilot.db")
    )
    path = Path(os.environ.get("AUTOPILOT_DB_PATH", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect():
    global _conn
    if _conn is None:
        _conn = db.connect(sqlite_path=_db_path())
        if not db.use_postgres():
            _conn.executescript("""
                CREATE TABLE IF NOT EXISTS venue_credentials (
                    user_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    enc_payload TEXT NOT NULL,
                    key_fingerprint TEXT,
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
                    overrides TEXT,
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
                CREATE TABLE IF NOT EXISTS autopilot_trades (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    arb_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    coin TEXT,
                    expiry TEXT,
                    contracts INTEGER,
                    edge_cents REAL,
                    locked_pnl REAL,
                    cost_total REAL,
                    live_mode INTEGER NOT NULL DEFAULT 0,
                    ok INTEGER NOT NULL DEFAULT 0,
                    errors TEXT,
                    legs TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    settled_at REAL,
                    pnl REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autopilot_log_user ON autopilot_log(user_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_autopilot_trades_user ON autopilot_trades(user_id, ts DESC);
            """)
            vc_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(venue_credentials)").fetchall()}
            if "key_fingerprint" not in vc_cols:
                _conn.execute("ALTER TABLE venue_credentials ADD COLUMN key_fingerprint TEXT")
            cfg_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(autopilot_config)").fetchall()}
            if "overrides" not in cfg_cols:
                _conn.execute("ALTER TABLE autopilot_config ADD COLUMN overrides TEXT")
            _conn.commit()
    return _conn


def _encrypt(user_id: str, data: dict) -> str:
    return seal_sensitive_payload(user_id, data)


def _decrypt(user_id: str, blob: str) -> dict:
    return open_sensitive_payload(user_id, blob)


def save_venue_credentials(user_id: str, venue: str, payload: dict) -> dict:
    if venue not in VENUES:
        raise ValueError(f"Unknown venue: {venue}")
    now = time.time()
    enc = _encrypt(user_id, payload)
    fingerprint = credential_fingerprint(user_id, payload)
    with _lock:
        _connect().execute(
            """INSERT INTO venue_credentials(user_id, venue, enc_payload, key_fingerprint, connected_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, venue) DO UPDATE SET
                 enc_payload=excluded.enc_payload,
                 key_fingerprint=excluded.key_fingerprint,
                 updated_at=excluded.updated_at""",
            (user_id, venue, enc, fingerprint or None, now, now),
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
    return _decrypt(user_id, row["enc_payload"])


def venue_status_light(user_id: str, venue: str) -> dict:
    """Connected flag without decrypting credentials (fast for polling)."""
    with _lock:
        row = _connect().execute(
            "SELECT updated_at FROM venue_credentials WHERE user_id=? AND venue=?",
            (user_id, venue),
        ).fetchone()
    if not row:
        return {"venue": venue, "connected": False}
    return {"venue": venue, "connected": True, "updated_at": row["updated_at"]}


def all_venue_status_light(user_id: str) -> list[dict]:
    return [venue_status_light(user_id, v) for v in VENUES]


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
        "overrides": {},
    }


def save_config(user_id: str, updates: dict) -> dict:
    current = get_config(user_id)
    current.update({k: v for k, v in updates.items() if k in (
        "strategy_id", "bankroll_usd", "live_mode", "max_exposure_pct", "reserve_pct", "running",
    )})
    if "overrides" in updates:
        current["overrides"] = updates["overrides"]
    overrides = _clean_overrides(current.get("overrides"))
    now = time.time()
    with _lock:
        _connect().execute(
            """INSERT INTO autopilot_config
               (user_id, strategy_id, bankroll_usd, live_mode, max_exposure_pct,
                reserve_pct, running, overrides, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 strategy_id=excluded.strategy_id,
                 bankroll_usd=excluded.bankroll_usd,
                 live_mode=excluded.live_mode,
                 max_exposure_pct=excluded.max_exposure_pct,
                 reserve_pct=excluded.reserve_pct,
                 running=excluded.running,
                 overrides=excluded.overrides,
                 updated_at=excluded.updated_at""",
            (
                user_id,
                current["strategy_id"],
                float(current["bankroll_usd"]),
                bool(current.get("live_mode")),
                current.get("max_exposure_pct"),
                float(current.get("reserve_pct", 30)),
                bool(current.get("running")),
                json.dumps(overrides) if overrides else None,
                now,
                now,
            ),
        )
        _connect().commit()
    return get_config(user_id)


def _config_row(row) -> dict:
    overrides = {}
    if "overrides" in row.keys() and row["overrides"]:
        try:
            overrides = _clean_overrides(json.loads(row["overrides"]))
        except (json.JSONDecodeError, TypeError):
            overrides = {}
    return {
        "user_id": row["user_id"],
        "strategy_id": row["strategy_id"],
        "bankroll_usd": float(row["bankroll_usd"]),
        "live_mode": bool(row["live_mode"]),
        "max_exposure_pct": row["max_exposure_pct"],
        "reserve_pct": float(row["reserve_pct"]),
        "running": bool(row["running"]),
        "overrides": overrides,
        "updated_at": row["updated_at"],
    }


def append_log(user_id: str, level: str, message: str, detail: Any = None) -> None:
    safe_detail = redact_secrets(detail) if detail is not None else None
    with _lock:
        _connect().execute(
            "INSERT INTO autopilot_log(user_id, ts, level, message, detail) VALUES (?, ?, ?, ?, ?)",
            (user_id, time.time(), level, message, json.dumps(safe_detail) if safe_detail is not None else None),
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


def save_trade(user_id: str, result: dict[str, Any]) -> dict:
    """Persist an execution result to trade history (Supabase or SQLite)."""
    arb_id = str(result.get("arb_id") or uuid.uuid4().hex[:12])
    contracts = int(result.get("contracts") or 0)
    edge = result.get("edge")
    locked = None
    cost_total = None
    if edge is not None and contracts:
        spread = max(0.0, float(edge) / 100.0)
        locked = round(spread * contracts, 4)
        cost_total = round(contracts * (1.0 - spread), 2)
    now = time.time()
    errors = result.get("errors") or []
    legs = result.get("legs") or {}
    ok = bool(result.get("ok"))
    row = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "arb_id": arb_id,
        "ts": float(result.get("ts") or now),
        "coin": result.get("coin"),
        "expiry": result.get("expiry"),
        "contracts": contracts,
        "edge_cents": edge,
        "locked_pnl": locked,
        "cost_total": cost_total,
        "live_mode": bool(result.get("live_mode")),
        "ok": ok,
        "errors": json.dumps(errors),
        "legs": json.dumps(legs),
        "status": "open" if ok else "failed",
        "settled_at": None,
        "pnl": None,
        "created_at": now,
    }
    with _lock:
        if db.use_postgres():
            _connect().execute(
                """INSERT INTO autopilot_trades
                   (id, user_id, arb_id, ts, coin, expiry, contracts, edge_cents, locked_pnl,
                    cost_total, live_mode, ok, errors, legs, status, settled_at, pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?, ?, ?, ?)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    row["id"], row["user_id"], row["arb_id"], row["ts"], row["coin"], row["expiry"],
                    row["contracts"], row["edge_cents"], row["locked_pnl"], row["cost_total"],
                    row["live_mode"], row["ok"], row["errors"], row["legs"],
                    row["status"], row["settled_at"], row["pnl"], row["created_at"],
                ),
            )
        else:
            _connect().execute(
                """INSERT OR IGNORE INTO autopilot_trades
                   (id, user_id, arb_id, ts, coin, expiry, contracts, edge_cents, locked_pnl,
                    cost_total, live_mode, ok, errors, legs, status, settled_at, pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], row["user_id"], row["arb_id"], row["ts"], row["coin"], row["expiry"],
                    row["contracts"], row["edge_cents"], row["locked_pnl"], row["cost_total"],
                    1 if row["live_mode"] else 0, 1 if row["ok"] else 0,
                    row["errors"], row["legs"], row["status"], row["settled_at"], row["pnl"], row["created_at"],
                ),
            )
        _connect().commit()
    return _trade_row_to_dict(row)


def recent_trades(user_id: str, limit: int = 100) -> list[dict]:
    with _lock:
        rows = _connect().execute(
            """SELECT id, arb_id, ts, coin, expiry, contracts, edge_cents, locked_pnl, cost_total,
                      live_mode, ok, errors, legs, status, settled_at, pnl, created_at
               FROM autopilot_trades WHERE user_id=? ORDER BY ts DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [_trade_row_to_dict({k: r[k] for k in r.keys()}) for r in rows]


def _trade_row_to_dict(row: dict) -> dict:
    out = dict(row)
    for key in ("errors", "legs"):
        val = out.get(key)
        if isinstance(val, str):
            try:
                out[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    out["live_mode"] = bool(out.get("live_mode"))
    out["ok"] = bool(out.get("ok"))
    return out


def trade_stats(user_id: str) -> dict:
    with _lock:
        rows = _connect().execute(
            """SELECT status, ok, locked_pnl, cost_total, pnl, live_mode
               FROM autopilot_trades WHERE user_id=?""",
            (user_id,),
        ).fetchall()
    filled = [r for r in rows if r["ok"]]
    failed = [r for r in rows if not r["ok"]]
    pending = sum(float(r["locked_pnl"] or 0) for r in filled if r["status"] == "open")
    realized = sum(float(r["pnl"] or 0) for r in filled if r["pnl"] is not None)
    deployed = sum(float(r["cost_total"] or 0) for r in filled if r["status"] == "open")
    return {
        "total_trades": len(rows),
        "filled": len(filled),
        "failed": len(failed),
        "open": sum(1 for r in filled if r["status"] == "open"),
        "settled": sum(1 for r in filled if r["status"] == "settled"),
        "pending_locked_usd": round(pending, 2),
        "realized_pnl_usd": round(realized, 2),
        "deployed_usd": round(deployed, 2),
        "live_fills": sum(1 for r in filled if r["live_mode"]),
        "paper_fills": sum(1 for r in filled if not r["live_mode"]),
    }
