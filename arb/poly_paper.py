"""Polymarket paper trading ledger — live order books, SQLite persistence.

Inspired by polymarket-paper-trader: walks the CLOB when available, applies
Polymarket's fee formula, tracks cash/positions/P&L locally (Polymarket has no
official demo account; this is the standard paper-trading approach).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import paper_settlement

log = logging.getLogger(__name__)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com/markets/"


def _db_path() -> str:
    default = (
        "/tmp/poly_paper.db"
        if os.environ.get("DYNO")
        else str(Path(__file__).parent.parent / "data" / "poly_paper.db")
    )
    path = Path(os.environ.get("POLY_PAPER_DB_PATH", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                ts REAL,
                market_id TEXT,
                token_id TEXT,
                side TEXT,
                outcome TEXT,
                shares REAL,
                avg_price REAL,
                cost_cents INTEGER,
                fee_cents INTEGER,
                slippage_bps REAL,
                arb_id TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                token_id TEXT PRIMARY KEY,
                market_id TEXT,
                outcome TEXT,
                shares REAL,
                avg_price REAL,
                cost_cents INTEGER
            );
        """)
        _conn.commit()
    return _conn


def _meta(key: str, default: str = "") -> str:
    row = _connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(key: str, value: str) -> None:
    _connect().execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    _connect().commit()


def _http_get(url: str) -> object:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "poly-paper/1.0"})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def poly_fee_cents(shares: float, price: float, fee_bps: int | None = None) -> int:
    """Polymarket fee: bps/10000 × min(p, 1-p) × shares (in cents)."""
    bps = fee_bps if fee_bps is not None else int(os.environ.get("POLY_FEE_BPS", "0"))
    if bps <= 0 or shares <= 0:
        return 0
    p = max(0.0, min(1.0, price))
    fee_dollars = (bps / 10_000.0) * min(p, 1 - p) * shares
    return int(math.ceil(fee_dollars * 100))


def _fetch_book(token_id: str) -> tuple[list[dict], list[dict]]:
    try:
        data = _http_get(f"{CLOB}/book?token_id={token_id}") or {}
        if data.get("error"):
            return [], []
        return data.get("bids") or [], data.get("asks") or []
    except Exception:
        return [], []


def _top_of_book(market_id: str, token_id: str, side: str) -> tuple[float, float] | None:
    """Return (price, size) for best level; fall back to gamma midpoint."""
    bids, asks = _fetch_book(token_id)
    levels = asks if side == "buy" else bids
    if levels:
        best = min(asks, key=lambda x: float(x["price"])) if side == "buy" else max(
            bids, key=lambda x: float(x["price"])
        )
        return float(best["price"]), float(best.get("size") or 1)

    try:
        m = _http_get(GAMMA + str(market_id)) or {}
        if side == "buy":
            ask = m.get("bestAsk")
            if ask is not None:
                return float(ask), float(os.environ.get("PAPER_BOOK_FALLBACK_SIZE", "100"))
        else:
            bid = m.get("bestBid")
            if bid is not None:
                return float(bid), float(os.environ.get("PAPER_BOOK_FALLBACK_SIZE", "100"))
    except Exception:
        pass
    return None


def walk_book(
    token_id: str,
    market_id: str,
    side: str,
    shares: float,
) -> dict[str, Any]:
    """Execute against live order book (level-by-level when depth exists)."""
    bids, asks = _fetch_book(token_id)
    levels = asks if side == "buy" else bids
    remaining = shares
    cost = 0.0
    filled = 0.0
    ref_price = None

    if levels:
        ordered = sorted(asks, key=lambda x: float(x["price"])) if side == "buy" else sorted(
            bids, key=lambda x: float(x["price"]), reverse=True
        )
        for lvl in ordered:
            if remaining <= 0:
                break
            px = float(lvl["price"])
            sz = float(lvl.get("size") or 0)
            if ref_price is None:
                ref_price = px
            take = min(remaining, sz) if sz > 0 else remaining
            cost += take * px
            filled += take
            remaining -= take
        if remaining > 0 and ref_price is not None:
            cost += remaining * ref_price
            filled += remaining
            remaining = 0
    else:
        top = _top_of_book(market_id, token_id, side)
        if not top:
            return {"error": "no liquidity"}
        ref_price, _ = top
        filled = shares
        cost = shares * ref_price

    avg = cost / filled if filled else 0
    slip_bps = 0.0
    if ref_price and avg:
        slip_bps = abs(avg - ref_price) / ref_price * 10_000
    fee = poly_fee_cents(filled, avg)
    return {
        "shares": filled,
        "avg_price": round(avg, 4),
        "cost_cents": int(round(cost * 100)),
        "fee_cents": fee,
        "slippage_bps": round(slip_bps, 2),
    }


def starting_balance_cents() -> int:
    raw = _meta("starting_balance_cents")
    if raw:
        return int(raw)
    start = int(float(os.environ.get("PAPER_STARTING_BALANCE", "10000")) * 100)
    _set_meta("starting_balance_cents", str(start))
    _set_meta("cash_cents", str(start))
    return start


def cash_cents() -> int:
    raw = _meta("cash_cents")
    if raw:
        return int(raw)
    return starting_balance_cents()


def _set_cash(cents: int) -> None:
    _set_meta("cash_cents", str(cents))


def buy(
    market_id: str,
    token_id: str,
    outcome: str,
    shares: float,
    arb_id: str = "",
) -> dict[str, Any]:
    """Paper-buy shares at live ask prices."""
    if paper_settlement.is_market_resolved(market_id):
        return {"error": "market already resolved — cannot open new positions"}

    fill = walk_book(token_id, market_id, "buy", shares)
    if fill.get("error"):
        return fill

    total = fill["cost_cents"] + fill["fee_cents"]
    with _lock:
        cash = cash_cents()
        if cash < total:
            return {"error": f"insufficient Poly paper cash (need {total}¢, have {cash}¢)"}

        _set_cash(cash - total)
        conn = _connect()
        row = conn.execute(
            "SELECT shares, avg_price, cost_cents FROM positions WHERE token_id=?",
            (token_id,),
        ).fetchone()
        if row:
            old_sh, old_avg, old_cost = row[0], row[1], row[2]
            new_sh = old_sh + fill["shares"]
            new_cost = old_cost + fill["cost_cents"]
            new_avg = new_cost / new_sh / 100.0
            conn.execute(
                "UPDATE positions SET shares=?, avg_price=?, cost_cents=? WHERE token_id=?",
                (new_sh, new_avg, new_cost, token_id),
            )
        else:
            conn.execute(
                "INSERT INTO positions(token_id, market_id, outcome, shares, avg_price, cost_cents) "
                "VALUES(?,?,?,?,?,?)",
                (token_id, market_id, outcome, fill["shares"], fill["avg_price"], fill["cost_cents"]),
            )

        tid = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO trades(id,ts,market_id,token_id,side,outcome,shares,avg_price,"
            "cost_cents,fee_cents,slippage_bps,arb_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, time.time(), market_id, token_id, "buy", outcome,
                fill["shares"], fill["avg_price"], fill["cost_cents"],
                fill["fee_cents"], fill["slippage_bps"], arb_id,
            ),
        )
        conn.commit()

    return {"trade_id": tid, "venue": "polymarket", **fill}


def settle_resolved() -> list[dict[str, Any]]:
    """Redeem positions in resolved markets."""
    settlements: list[dict[str, Any]] = []
    conn = _connect()
    positions = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE shares > 0")]
    if not positions:
        return settlements

    resolution_cache: dict[str, dict | None] = {}
    with _lock:
        cash = cash_cents()
        for pos in positions:
            mid = str(pos["market_id"])
            if mid not in resolution_cache:
                resolution_cache[mid] = paper_settlement.get_market_resolution(mid)
            res = resolution_cache[mid]
            if not res:
                continue

            token_id = str(pos["token_id"])
            payout_per = paper_settlement.payout_cents_per_share(res, token_id)
            if payout_per is None:
                continue

            shares = float(pos["shares"])
            payout_total = int(round(shares * payout_per))
            cost = int(pos.get("cost_cents") or 0)
            cash += payout_total

            conn.execute("DELETE FROM positions WHERE token_id=?", (token_id,))
            tid = uuid.uuid4().hex[:12]
            won = payout_per >= 100
            conn.execute(
                "INSERT INTO trades(id,ts,market_id,token_id,side,outcome,shares,avg_price,"
                "cost_cents,fee_cents,slippage_bps,arb_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid, time.time(), mid, token_id, "settle", pos.get("outcome") or "",
                    shares, payout_per / 100.0, payout_total, 0, 0.0,
                    "won" if won else "lost",
                ),
            )
            settlements.append({
                "trade_id": tid,
                "market_id": mid,
                "question": res.get("question") or "",
                "outcome": pos.get("outcome") or "",
                "shares": shares,
                "payout_cents": payout_total,
                "pnl_cents": payout_total - cost,
                "won": won,
            })

        if settlements:
            _set_cash(cash)
            conn.commit()

    return settlements


def mark_to_market() -> int:
    """Position value in cents at current mids."""
    total = 0
    conn = _connect()
    for row in conn.execute("SELECT token_id, market_id, shares FROM positions"):
        token_id, market_id, shares = row[0], row[1], row[2]
        res = paper_settlement.get_market_resolution(str(market_id))
        if res:
            payout_per = paper_settlement.payout_cents_per_share(res, str(token_id))
            if payout_per is not None:
                total += int(round(shares * payout_per))
                continue
        top = _top_of_book(market_id, token_id, "sell")
        if top:
            total += int(round(shares * top[0] * 100))
        else:
            pos = conn.execute(
                "SELECT cost_cents FROM positions WHERE token_id=?", (token_id,)
            ).fetchone()
            total += int(pos[0]) if pos else 0
    return total


def reset() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM positions")
        conn.commit()
        start = int(float(os.environ.get("PAPER_STARTING_BALANCE", "10000")) * 100)
        _set_meta("starting_balance_cents", str(start))
        _set_meta("cash_cents", str(start))
        _set_meta("reset_at", str(time.time()))
    return account_summary()


def recent_trades(limit: int = 50) -> list[dict]:
    rows = _connect().execute(
        "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def account_summary() -> dict[str, Any]:
    settlements = settle_resolved()
    start = starting_balance_cents()
    cash = cash_cents()
    pos_val = mark_to_market()
    equity = cash + pos_val
    positions = [
        dict(r)
        for r in _connect().execute("SELECT * FROM positions WHERE shares > 0")
    ]
    return {
        "venue": "polymarket",
        "mode": "paper",
        "db_path": _db_path(),
        "starting_balance_cents": start,
        "cash_cents": cash,
        "positions_value_cents": pos_val,
        "equity_cents": equity,
        "pnl_cents": equity - start,
        "positions": positions,
        "positions_count": len(positions),
        "trade_count": _connect().execute("SELECT COUNT(*) FROM trades").fetchone()[0],
        "recent_settlements": settlements,
    }
