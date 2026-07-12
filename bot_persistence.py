"""Persistence layer for crypto arb bot state in Supabase."""

from __future__ import annotations

import json
import time
from typing import Any

import db


def save_bot_state(
    strategy_id: str,
    life: int,
    cash_cents: int,
    realized_cents: int,
    settled_count: int,
    wins: int,
    total_injected_cents: int,
    lifetime_realized_cents: int,
) -> None:
    """Save bot state to database."""
    conn = db.connect()
    try:
        now = time.time()
        if db.use_postgres():
            sql = """
                INSERT INTO bot_state (
                    strategy_id, life, cash_cents, realized_cents, settled_count, wins,
                    total_injected_cents, lifetime_realized_cents, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    life=EXCLUDED.life,
                    cash_cents=EXCLUDED.cash_cents,
                    realized_cents=EXCLUDED.realized_cents,
                    settled_count=EXCLUDED.settled_count,
                    wins=EXCLUDED.wins,
                    total_injected_cents=EXCLUDED.total_injected_cents,
                    lifetime_realized_cents=EXCLUDED.lifetime_realized_cents,
                    updated_at=EXCLUDED.updated_at
            """
            conn.execute(sql, (
                strategy_id, life, cash_cents, realized_cents, settled_count, wins,
                total_injected_cents, lifetime_realized_cents, now
            ))
        else:
            conn.execute("DELETE FROM bot_state WHERE strategy_id = ?", (strategy_id,))
            sql = """
                INSERT INTO bot_state (
                    strategy_id, life, cash_cents, realized_cents, settled_count, wins,
                    total_injected_cents, lifetime_realized_cents, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(sql, (
                strategy_id, life, cash_cents, realized_cents, settled_count, wins,
                total_injected_cents, lifetime_realized_cents, now
            ))
        conn.commit()
    finally:
        db.release(conn)


def load_bot_state(strategy_id: str) -> dict[str, Any] | None:
    """Load bot state from database."""
    conn = db.connect()
    try:
        sql = "SELECT * FROM bot_state WHERE strategy_id = ?"
        cursor = conn.execute(sql, (strategy_id,))
        row = cursor.fetchone()
        if row:
            return dict(row._data) if hasattr(row, '_data') else dict(row)
        return None
    finally:
        db.release(conn)


def save_position(
    strategy_id: str,
    position_id: str,
    coin: str,
    expiry: str,
    strike: float | None,
    yes_venue: str | None,
    no_venue: str | None,
    yes_cost: float,
    no_cost: float,
    contracts: int,
    cost_cents: int,
    locked_cents: int,
    payout_cents: int,
    gap: float,
    entry_ts: float,
    expiry_ts: float,
    data: dict,
) -> None:
    """Save or update an open position."""
    conn = db.connect()
    try:
        now = time.time()
        if db.use_postgres():
            sql = """
                INSERT INTO bot_positions (
                    id, strategy_id, coin, expiry, strike, yes_venue, no_venue,
                    yes_cost, no_cost, contracts, cost_cents, locked_cents,
                    payout_cents, gap, entry_ts, expiry_ts, data,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    locked_cents=EXCLUDED.locked_cents,
                    payout_cents=EXCLUDED.payout_cents,
                    data=EXCLUDED.data,
                    updated_at=EXCLUDED.updated_at
            """
            conn.execute(sql, (
                position_id, strategy_id, coin, expiry, strike, yes_venue, no_venue,
                yes_cost, no_cost, contracts, cost_cents, locked_cents,
                payout_cents, gap, entry_ts, expiry_ts, json.dumps(data),
                now, now
            ))
        else:
            conn.execute("DELETE FROM bot_positions WHERE id = ?", (position_id,))
            sql = """
                INSERT INTO bot_positions (
                    id, strategy_id, coin, expiry, strike, yes_venue, no_venue,
                    yes_cost, no_cost, contracts, cost_cents, locked_cents,
                    payout_cents, gap, entry_ts, expiry_ts, data,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(sql, (
                position_id, strategy_id, coin, expiry, strike, yes_venue, no_venue,
                yes_cost, no_cost, contracts, cost_cents, locked_cents,
                payout_cents, gap, entry_ts, expiry_ts, json.dumps(data),
                now, now
            ))
        conn.commit()
    finally:
        db.release(conn)


def remove_position(position_id: str) -> None:
    """Remove a closed position."""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM bot_positions WHERE id = ?", (position_id,))
        conn.commit()
    finally:
        db.release(conn)


def load_positions(strategy_id: str) -> list[dict[str, Any]]:
    """Load all open positions for a strategy."""
    conn = db.connect()
    try:
        sql = "SELECT * FROM bot_positions WHERE strategy_id = ? ORDER BY entry_ts"
        cursor = conn.execute(sql, (strategy_id,))
        rows = cursor.fetchall()
        positions = []
        for row in rows:
            data = dict(row._data) if hasattr(row, '_data') else dict(row)
            if isinstance(data.get('data'), str):
                data['data'] = json.loads(data['data'])
            positions.append(data)
        return positions
    finally:
        db.release(conn)


def save_trade(
    trade_id: str,
    strategy_id: str,
    status: str,
    coin: str | None,
    expiry: str | None,
    strike: float | None,
    contracts: int | None,
    cost_total: float | None,
    locked_pnl: float | None,
    pnl: float | None,
    spread: float | None,
    entry_ts: float | None,
    settled_at: float | None,
    data: dict,
) -> None:
    """Save or update a trade."""
    conn = db.connect()
    try:
        now = time.time()
        if db.use_postgres():
            sql = """
                INSERT INTO bot_trades (
                    id, strategy_id, status, coin, expiry, strike, contracts,
                    cost_total, locked_pnl, pnl, spread, entry_ts, settled_at,
                    data, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    status=EXCLUDED.status,
                    locked_pnl=EXCLUDED.locked_pnl,
                    pnl=EXCLUDED.pnl,
                    settled_at=EXCLUDED.settled_at,
                    data=EXCLUDED.data,
                    updated_at=EXCLUDED.updated_at
            """
            conn.execute(sql, (
                trade_id, strategy_id, status, coin, expiry, strike, contracts,
                cost_total, locked_pnl, pnl, spread, entry_ts, settled_at,
                json.dumps(data), now, now
            ))
        else:
            conn.execute("DELETE FROM bot_trades WHERE id = ?", (trade_id,))
            sql = """
                INSERT INTO bot_trades (
                    id, strategy_id, status, coin, expiry, strike, contracts,
                    cost_total, locked_pnl, pnl, spread, entry_ts, settled_at,
                    data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(sql, (
                trade_id, strategy_id, status, coin, expiry, strike, contracts,
                cost_total, locked_pnl, pnl, spread, entry_ts, settled_at,
                json.dumps(data), now, now
            ))
        conn.commit()
    finally:
        db.release(conn)


def load_trades(strategy_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Load trades for a strategy."""
    conn = db.connect()
    try:
        sql = """
            SELECT * FROM bot_trades
            WHERE strategy_id = ?
            ORDER BY entry_ts DESC
            LIMIT ?
        """
        cursor = conn.execute(sql, (strategy_id, limit))
        rows = cursor.fetchall()
        trades = []
        for row in rows:
            data = dict(row._data) if hasattr(row, '_data') else dict(row)
            if isinstance(data.get('data'), str):
                data['data'] = json.loads(data['data'])
            trades.append(data)
        return list(reversed(trades))
    finally:
        db.release(conn)


def save_bust(
    strategy_id: str,
    life: int,
    ts: float,
    final_cash: float,
    realized: float,
    settled: int,
    wins: int,
) -> None:
    """Record a bust event."""
    conn = db.connect()
    try:
        sql = """
            INSERT INTO bot_busts (
                strategy_id, life, ts, final_cash, realized, settled, wins,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn.execute(sql, (
            strategy_id, life, ts, final_cash, realized, settled, wins,
            time.time()
        ))
        conn.commit()
    finally:
        db.release(conn)


def load_busts(strategy_id: str) -> list[dict[str, Any]]:
    """Load bust history for a strategy."""
    conn = db.connect()
    try:
        sql = "SELECT * FROM bot_busts WHERE strategy_id = ? ORDER BY life"
        cursor = conn.execute(sql, (strategy_id,))
        rows = cursor.fetchall()
        busts = []
        for row in rows:
            busts.append(dict(row._data) if hasattr(row, '_data') else dict(row))
        return busts
    finally:
        db.release(conn)


def save_equity_point(
    strategy_id: str,
    ts: float,
    equity: float,
    life: int | None = None,
) -> None:
    """Save an equity curve data point."""
    conn = db.connect()
    try:
        sql = """
            INSERT INTO bot_equity_curve (
                strategy_id, ts, equity, life, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """
        conn.execute(sql, (strategy_id, ts, equity, life, time.time()))
        conn.commit()
    finally:
        db.release(conn)


def load_equity_curve(strategy_id: str, limit: int = 180) -> list[dict[str, Any]]:
    """Load recent equity curve points."""
    conn = db.connect()
    try:
        sql = """
            SELECT ts, equity, life FROM bot_equity_curve
            WHERE strategy_id = ?
            ORDER BY ts DESC
            LIMIT ?
        """
        cursor = conn.execute(sql, (strategy_id, limit))
        rows = cursor.fetchall()
        points = []
        for row in rows:
            data = dict(row._data) if hasattr(row, '_data') else dict(row)
            points.append({"ts": data["ts"], "equity": data["equity"], "life": data.get("life")})
        return list(reversed(points))
    finally:
        db.release(conn)
