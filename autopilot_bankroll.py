"""Bankroll dashboard — aggregates live execution, venue balances, and paper benchmarks."""

from __future__ import annotations

import time
from typing import Any

import autopilot_engine
import autopilot_executor
import autopilot_store
import bots_hub
import crypto_arb
import crypto_arb_bot
import db


def _venue_cash_usd(venue: str, bal: dict) -> float | None:
    if not bal or bal.get("error") or bal.get("connected") is False:
        return None
    if venue == "kalshi":
        cents = bal.get("equity_cents")
        if cents is None:
            cents = bal.get("cash_cents")
        return round((cents or 0) / 100.0, 2)
    if venue == "cryptocom":
        return round(float(bal.get("equity_usd") or bal.get("cash_usd") or 0), 2)
    if venue == "polymarket":
        if bal.get("usdc") is not None:
            return round(float(bal["usdc"]), 2)
        raw = bal.get("raw")
        if isinstance(raw, dict):
            for key in ("balance", "usdc", "collateral", "available"):
                if raw.get(key) is not None:
                    return round(float(raw[key]), 2)
    return None


def _parse_execution(detail: dict, log: dict) -> dict | None:
    if not isinstance(detail, dict) or not detail.get("arb_id"):
        return None
    contracts = int(detail.get("contracts") or 0)
    edge_cents = detail.get("edge")
    locked = None
    spread = None
    if edge_cents is not None:
        spread = max(0.0, float(edge_cents) / 100.0)
        if contracts:
            locked = round(spread * contracts, 4)
    ts = float(detail.get("ts") or log.get("ts") or time.time())
    legs_raw = detail.get("legs") or {}
    legs: list[dict] = []
    for key, leg in legs_raw.items():
        if not isinstance(leg, dict):
            continue
        if "kalshi" in key:
            venue = "kalshi"
        elif "poly" in key:
            venue = "polymarket"
        else:
            venue = "cryptocom"
        legs.append({
            "key": key,
            "venue": venue,
            "paper": bool(leg.get("paper")),
            "error": leg.get("error"),
        })
    cost_total = None
    if contracts and spread is not None:
        cost_total = round(contracts * (1.0 - spread), 2)
    return {
        "id": detail["arb_id"],
        "status": "filled" if detail.get("ok") else "failed",
        "coin": detail.get("coin"),
        "expiry": detail.get("expiry"),
        "contracts": contracts,
        "locked_pnl": locked,
        "spread": spread,
        "spread_cents": round(spread * 100, 2) if spread is not None else None,
        "edge_cents": edge_cents,
        "entry_ts": ts,
        "live_mode": bool(detail.get("live_mode")),
        "ok": bool(detail.get("ok")),
        "errors": list(detail.get("errors") or []),
        "legs": legs,
        "cost_total": cost_total,
        "log_level": log.get("level"),
        "log_message": log.get("message"),
    }


def _execution_from_trade(trade: dict) -> dict:
    return {
        "id": trade.get("arb_id") or trade.get("id"),
        "status": "filled" if trade.get("ok") else "failed",
        "coin": trade.get("coin"),
        "expiry": trade.get("expiry"),
        "contracts": trade.get("contracts"),
        "locked_pnl": trade.get("locked_pnl"),
        "spread": (float(trade["edge_cents"]) / 100.0) if trade.get("edge_cents") is not None else None,
        "spread_cents": trade.get("edge_cents"),
        "edge_cents": trade.get("edge_cents"),
        "entry_ts": trade.get("ts"),
        "live_mode": trade.get("live_mode"),
        "ok": trade.get("ok"),
        "errors": trade.get("errors") or [],
        "legs": trade.get("legs") or [],
        "cost_total": trade.get("cost_total"),
        "trade_status": trade.get("status"),
        "pnl": trade.get("pnl"),
    }


def _executions_from_logs(logs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for log in logs:
        detail = log.get("detail")
        if not detail:
            continue
        ex = _parse_execution(detail, log)
        if not ex or ex["id"] in seen:
            continue
        seen.add(ex["id"])
        out.append(ex)
    out.sort(key=lambda row: row.get("entry_ts") or 0, reverse=True)
    return out


def _trade_snap(executions: list[dict]) -> dict:
    """Shape executions into paper-bot trade list for heatmap helpers."""
    trades = []
    for ex in executions:
        if not ex.get("ok"):
            continue
        trades.append({
            "id": ex["id"],
            "status": "open",
            "coin": ex.get("coin"),
            "expiry": ex.get("expiry"),
            "contracts": ex.get("contracts"),
            "locked_pnl": ex.get("locked_pnl"),
            "spread": ex.get("spread"),
            "spread_cents": ex.get("spread_cents"),
            "cost_total": ex.get("cost_total"),
            "entry_ts": ex.get("entry_ts"),
            "legs": [
                {
                    "venue": leg.get("venue"),
                    "side": "yes" if leg.get("key", "").endswith("yes") else "no",
                    "cost_total": (ex.get("cost_total") or 0) / 2 if ex.get("cost_total") else None,
                }
                for leg in ex.get("legs") or []
            ],
        })
    return {"trades": trades}


def _equity_curve(executions: list[dict], bankroll: float) -> list[dict]:
    filled = sorted(
        [e for e in executions if e.get("ok")],
        key=lambda row: row.get("entry_ts") or 0,
    )
    if not filled:
        return [{"ts": time.time(), "equity": bankroll}]
    curve = [{"ts": filled[0]["entry_ts"] - 60, "equity": bankroll}]
    equity = bankroll
    for ex in filled:
        equity += float(ex.get("locked_pnl") or 0)
        curve.append({"ts": ex["entry_ts"], "equity": round(equity, 2)})
    curve.append({"ts": time.time(), "equity": round(equity, 2)})
    return curve[-120:]


def _risk_from_executions(executions: list[dict], bankroll: float, pending: float) -> dict:
    ok = [e for e in executions if e.get("ok")]
    failed = [e for e in executions if not e.get("ok")]
    costs = [float(e.get("cost_total") or 0) for e in ok]
    deployed = sum(costs)
    equity = bankroll + pending
    spreads = [float(e["spread_cents"]) for e in ok if e.get("spread_cents") is not None]
    return {
        "execution_count": len(executions),
        "filled_count": len(ok),
        "failed_count": len(failed),
        "fill_rate_pct": round(len(ok) / len(executions) * 100, 1) if executions else None,
        "total_contracts": sum(int(e.get("contracts") or 0) for e in ok),
        "avg_spread_cents": round(sum(spreads) / len(spreads), 2) if spreads else None,
        "open_exposure_pct": round(deployed / equity * 100, 2) if equity > 0 and deployed else None,
        "open_concentration_pct": (
            round(max(costs) / deployed * 100, 2) if costs and deployed > 0 else None
        ),
        "avg_cost_per_fill": round(deployed / len(ok), 2) if ok and deployed else None,
        "live_fill_count": sum(1 for e in ok if e.get("live_mode")),
        "paper_fill_count": sum(1 for e in ok if not e.get("live_mode")),
    }


def _venue_rows(user_id: str, balances: dict) -> list[dict]:
    rows = []
    for venue in autopilot_store.VENUES:
        status = autopilot_store.venue_status(user_id, venue)
        bal = (balances.get("venues") or {}).get(venue) or {}
        cash = _venue_cash_usd(venue, bal)
        rows.append({
            "venue": venue,
            "label": bots_hub.VENUE_LABELS.get(venue, venue),
            "connected": bool(status.get("connected")),
            "masked": status,
            "cash_usd": cash,
            "ready": venue in (balances.get("ready") or []),
            "error": bal.get("error"),
            "demo": bal.get("demo"),
            "raw_balance": {
                k: v for k, v in bal.items()
                if k not in ("error",) and not isinstance(v, (dict, list))
            },
        })
    return rows


def _paper_benchmark(strategy_id: str) -> dict | None:
    bot = crypto_arb_bot.get_strategy_bot(strategy_id)
    if not bot:
        return None
    snap = bot.snapshot()
    meta = crypto_arb_bot.STRATEGIES.get(strategy_id) or {}
    spread_stats, avg_spread = bots_hub._spread_breakdown(snap)  # noqa: SLF001
    risk = snap.get("risk_metrics") or {}
    return {
        "strategy_id": strategy_id,
        "label": meta.get("label", strategy_id),
        "equity": snap.get("equity"),
        "cash": snap.get("cash"),
        "deployed": snap.get("deployed"),
        "confirmed_pnl": snap.get("confirmed_pnl"),
        "locked_pending": snap.get("locked_pending"),
        "return_pct": snap.get("return_pct"),
        "open_count": snap.get("open_count"),
        "settled_count": snap.get("settled_count"),
        "wins": snap.get("wins"),
        "win_rate": (
            round(int(snap.get("wins") or 0) / int(snap.get("settled_count") or 1) * 100, 1)
            if snap.get("settled_count") else None
        ),
        "life": snap.get("life"),
        "busts": snap.get("busts"),
        "avg_spread_cents": avg_spread,
        "spread_stats": spread_stats,
        "coin_stats": bots_hub._coin_breakdown(snap),  # noqa: SLF001
        "expiry_stats": bots_hub._expiry_breakdown(snap),  # noqa: SLF001
        "calibration": bots_hub._calibration(snap),  # noqa: SLF001
        "funnel": bots_hub._funnel_payload(snap),  # noqa: SLF001
        "risk_metrics": risk,
        "equity_curve": bots_hub._compact_equity_curve(snap.get("equity_curve")),  # noqa: SLF001
        "bankroll_note": "$50 independent paper bankroll",
    }


def bankroll_payload(user_id: str) -> dict[str, Any]:
    cfg = autopilot_store.get_config(user_id)
    runner = autopilot_engine.get_runner(user_id)
    status = runner.status()
    balances = status.get("balances") or autopilot_executor.venue_balances(user_id)
    logs = autopilot_store.recent_logs(user_id, limit=250)
    stored_trades = autopilot_store.recent_trades(user_id, limit=200)
    trade_stats = autopilot_store.trade_stats(user_id)
    executions = _executions_from_logs(logs)
    if stored_trades:
        executions = [_execution_from_trade(t) for t in stored_trades] or executions
    trade_snap = _trade_snap(executions)

    bankroll = float(cfg.get("bankroll_usd") or 300)
    reserve_pct = float(cfg.get("reserve_pct") or 30)
    deployable = round(bankroll * (1.0 - reserve_pct / 100.0), 2)
    strategy_id = str(cfg.get("strategy_id") or "half_kelly")

    venue_rows = _venue_rows(user_id, balances)
    venue_cash_total = sum(v["cash_usd"] or 0 for v in venue_rows if v.get("cash_usd") is not None)
    cash_gap = round(venue_cash_total - bankroll, 2) if venue_cash_total else None

    pending_locked = round(sum(float(e.get("locked_pnl") or 0) for e in executions if e.get("ok")), 2)
    filled_cost = round(sum(float(e.get("cost_total") or 0) for e in executions if e.get("ok")), 2)

    spread_stats, avg_spread = bots_hub._spread_breakdown(trade_snap)  # noqa: SLF001
    coin_stats = bots_hub._coin_breakdown(trade_snap)  # noqa: SLF001
    expiry_stats = bots_hub._expiry_breakdown(trade_snap)  # noqa: SLF001
    calibration = bots_hub._calibration(trade_snap)  # noqa: SLF001
    venue_allocation = bots_hub._venue_allocation(trade_snap.get("trades") or [], bankroll)  # noqa: SLF001

    stats = status.get("stats") or {}
    runner_stats = {
        **stats,
        "thread_alive": status.get("thread_alive"),
        "attempt_fill_rate_pct": (
            round(int(stats.get("fills") or 0) / int(stats.get("attempts") or 1) * 100, 2)
            if stats.get("attempts") else None
        ),
        "cycles_per_fill": (
            round(int(stats.get("cycles") or 0) / max(int(stats.get("fills") or 0), 1), 1)
            if stats.get("fills") else None
        ),
    }

    catalog = {row["id"]: row for row in autopilot_engine.strategy_catalog()}
    strategy_meta = catalog.get(strategy_id, {})

    arb_snap = crypto_arb.snapshot()
    scanner = {
        "opportunity_count": len(arb_snap.get("opportunities") or []),
        "stats": arb_snap.get("stats") or {},
        "updated": arb_snap.get("updated"),
    }

    equity_curve = _equity_curve(executions, bankroll)
    exec_risk = _risk_from_executions(executions, bankroll, pending_locked)

    return_pct = round(pending_locked / bankroll * 100, 3) if bankroll else None

    return {
        "updated_at": time.time(),
        "config": cfg,
        "strategy": strategy_meta,
        "summary": {
            "bankroll_usd": bankroll,
            "reserve_pct": reserve_pct,
            "deployable_usd": deployable,
            "reserve_usd": round(bankroll - deployable, 2),
            "live_mode": bool(cfg.get("live_mode")),
            "running": bool(cfg.get("running")),
            "venue_cash_total_usd": round(venue_cash_total, 2) if venue_cash_total else None,
            "cash_gap_usd": cash_gap,
            "cash_gap_pct": round(cash_gap / bankroll * 100, 1) if cash_gap is not None and bankroll else None,
            "pending_locked_usd": pending_locked,
            "deployed_cost_usd": filled_cost,
            "equity_estimate_usd": round(bankroll + pending_locked, 2),
            "return_estimate_pct": return_pct,
            "venues_connected": sum(1 for v in venue_rows if v.get("connected")),
            "venues_ready": len(balances.get("ready") or []),
            "avg_spread_cents": avg_spread,
        },
        "runner": runner_stats,
        "venues": venue_rows,
        "venue_allocation": venue_allocation,
        "executions": executions,
        "spread_stats": spread_stats,
        "coin_stats": coin_stats,
        "expiry_stats": expiry_stats,
        "calibration": calibration,
        "execution_risk": exec_risk,
        "equity_curve": equity_curve,
        "paper_benchmark": _paper_benchmark(strategy_id),
        "scanner": scanner,
        "activity": logs[:40],
        "stored_trades": stored_trades,
        "trade_stats": trade_stats,
        "db": db.backend_label(),
    }
