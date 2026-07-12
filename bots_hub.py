"""Side-by-side comparison data for every crypto arb sizing strategy."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import crypto_arb_bot


SPREAD_BUCKETS = (
    {"id": "micro", "label": "< 2¢", "min": 0.0, "max": 0.02},
    {"id": "small", "label": "2–5¢", "min": 0.02, "max": 0.05},
    {"id": "medium", "label": "5–10¢", "min": 0.05, "max": 0.10},
    {"id": "wide", "label": "10¢+", "min": 0.10, "max": None},
)
EXPIRY_BUCKETS = (
    {"id": "lt_1m", "label": "< 1m", "min": 0, "max": 60},
    {"id": "m1_5", "label": "1–5m", "min": 60, "max": 300},
    {"id": "m5_15", "label": "5–15m", "min": 300, "max": 900},
    {"id": "gt_15m", "label": "15m+", "min": 900, "max": None},
)
VENUES = ("cryptocom", "kalshi", "polymarket")
VENUE_LABELS = {
    "cryptocom": "crypto.com",
    "kalshi": "Kalshi",
    "polymarket": "Polymarket",
}
_ALLOC_PATH = Path(__file__).resolve().parent / "data" / "venue_allocator_history.json"
_ALLOC_LOCK = threading.RLock()
_ALLOC_HISTORY: dict[str, dict] = {}


def _load_allocation_history() -> None:
    global _ALLOC_HISTORY
    try:
        value = json.loads(_ALLOC_PATH.read_text())
        if isinstance(value, dict):
            _ALLOC_HISTORY = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _ALLOC_HISTORY = {}


def _trade_history_key(strategy: str, trade: dict) -> str:
    trade_id = str(trade.get("id") or "")
    natural_id = "|".join(trade_id.split("|")[:3]) or (
        f"{trade.get('coin')}|{trade.get('expiry')}|{trade.get('strike')}"
    )
    return f"{strategy}|{natural_id}"


def _record_allocation_history(snapshots: dict[str, dict]) -> dict[str, list[dict]]:
    changed = False
    current_keys: set[str] = set()
    with _ALLOC_LOCK:
        for strategy, snapshot in snapshots.items():
            for trade in snapshot.get("trades") or []:
                key = _trade_history_key(strategy, trade)
                current_keys.add(key)
                compact = {
                    "id": trade.get("id"),
                    "strategy": strategy,
                    "status": trade.get("status"),
                    "entry_ts": trade.get("entry_ts"),
                    "settled_at": trade.get("settled_at"),
                    "legs": [
                        {
                            "venue": leg.get("venue"),
                            "cost_total": leg.get("cost_total"),
                            "payout": leg.get("payout"),
                        }
                        for leg in (trade.get("legs") or [])
                    ],
                }
                if _ALLOC_HISTORY.get(key) != compact:
                    _ALLOC_HISTORY[key] = compact
                    changed = True
        for key, trade in _ALLOC_HISTORY.items():
            if trade.get("status") == "open" and key not in current_keys:
                trade["status"] = "interrupted"
                changed = True
        if changed:
            try:
                _ALLOC_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp = _ALLOC_PATH.with_suffix(".tmp")
                temp.write_text(json.dumps(_ALLOC_HISTORY, separators=(",", ":")))
                temp.replace(_ALLOC_PATH)
            except OSError:
                pass
        by_strategy = {strategy: [] for strategy in snapshots}
        for trade in _ALLOC_HISTORY.values():
            strategy = trade.get("strategy")
            if strategy in by_strategy:
                by_strategy[strategy].append(dict(trade))
        return by_strategy


_load_allocation_history()


def _venue_allocation(trades: list[dict], bankroll: float) -> dict:
    stats = {
        venue: {
            "venue": venue,
            "label": VENUE_LABELS[venue],
            "open_committed": 0.0,
            "historical_notional": 0.0,
            "weighted_notional": 0.0,
            "settlement_cash_flow": 0.0,
            "legs": 0,
        }
        for venue in VENUES
    }
    ordered = sorted(
        trades,
        key=lambda trade: float(
            trade.get("entry_ts") or trade.get("settled_at") or 0
        ),
        reverse=True,
    )
    for index, trade in enumerate(ordered):
        recency_weight = 0.94 ** index
        is_open = trade.get("status") == "open"
        for leg in trade.get("legs") or []:
            venue = leg.get("venue")
            if venue not in stats:
                continue
            cost = float(leg.get("cost_total") or 0)
            payout = float(leg.get("payout") or 0)
            row = stats[venue]
            row["legs"] += 1
            row["historical_notional"] += cost
            row["weighted_notional"] += cost * recency_weight
            if is_open:
                row["open_committed"] += cost
            else:
                row["settlement_cash_flow"] += payout - cost

    open_total = sum(row["open_committed"] for row in stats.values())
    history_total = sum(row["weighted_notional"] for row in stats.values())
    if history_total <= 0:
        adaptive = {venue: 1 / len(VENUES) for venue in VENUES}
    else:
        history_share = {
            venue: stats[venue]["weighted_notional"] / history_total
            for venue in VENUES
        }
        if open_total > 0:
            open_share = {
                venue: stats[venue]["open_committed"] / open_total
                for venue in VENUES
            }
            adaptive = {
                venue: 0.65 * open_share[venue] + 0.35 * history_share[venue]
                for venue in VENUES
            }
        else:
            adaptive = history_share

    # Keep 5% operational liquidity at every venue; allocate the remaining 85%
    # to observed leg demand. This prevents a sparse early sample from assigning
    # zero capital to a venue and blocking the next otherwise valid pair.
    target = {venue: 5.0 + 85.0 * adaptive[venue] for venue in VENUES}
    rounded = {venue: round(value, 1) for venue, value in target.items()}
    rounding_error = round(100.0 - sum(rounded.values()), 1)
    if rounding_error:
        largest = max(rounded, key=rounded.get)
        rounded[largest] = round(rounded[largest] + rounding_error, 1)

    bankroll = max(0.0, float(bankroll))
    rows = []
    for venue in VENUES:
        row = stats[venue]
        row.update({
            "recommended_pct": rounded[venue],
            "recommended_amount": round(bankroll * rounded[venue] / 100, 2),
            "per_1000": round(10 * rounded[venue], 2),
            "open_committed": round(row["open_committed"], 2),
            "historical_notional": round(row["historical_notional"], 2),
            "weighted_notional": round(row["weighted_notional"], 2),
            "settlement_cash_flow": round(row["settlement_cash_flow"], 2),
        })
        rows.append(row)

    sample_legs = sum(row["legs"] for row in rows)
    return {
        "venues": rows,
        "bankroll": round(bankroll, 2),
        "sample_legs": sample_legs,
        "confidence_pct": round(min(100.0, sample_legs / 40 * 100), 1),
        "method": (
            "5% minimum reserve per venue; remaining 85% follows a blend of "
            "65% current open leg commitments and 35% recency-weighted leg cost."
        ),
    }


def _empty_heat_cell() -> dict:
    return {
        "trades": 0,
        "open": 0,
        "settled": 0,
        "contracts": 0,
        "pnl": 0.0,
        "pending_pnl": 0.0,
        "wins": 0,
        "win_rate": None,
    }


def _finalize_heat_cells(stats: dict) -> None:
    for cell in stats.values():
        cell["pnl"] = round(cell["pnl"], 2)
        cell["pending_pnl"] = round(cell["pending_pnl"], 2)
        cell["win_rate"] = (
            round(cell["wins"] / cell["settled"] * 100, 1)
            if cell["settled"] else None
        )


def _trade_spread(trade: dict) -> float | None:
    contracts = int(trade.get("contracts") or 0)
    spread = trade.get("spread")
    if spread is None and contracts:
        locked = trade.get("locked_pnl")
        if locked is not None:
            spread = float(locked) / contracts
    if spread is None:
        legs = trade.get("legs") or []
        if len(legs) >= 2:
            spread = 1 - sum(float(leg.get("cost_per") or 0) for leg in legs)
    if spread is None:
        return None
    return max(0.0, float(spread))


def _bucket_for_spread(spread: float) -> dict:
    return next((
        item for item in SPREAD_BUCKETS
        if spread >= item["min"]
        and (item["max"] is None or spread < item["max"])
    ), SPREAD_BUCKETS[-1])


def _accumulate_trade_cell(cell: dict, trade: dict) -> None:
    contracts = int(trade.get("contracts") or 0)
    is_open = trade.get("status") == "open"
    cell["trades"] += 1
    cell["open" if is_open else "settled"] += 1
    cell["contracts"] += contracts
    if is_open:
        cell["pending_pnl"] += float(trade.get("locked_pnl") or 0)
    else:
        cell["pnl"] += float(trade.get("pnl") or 0)
    if not is_open and float(trade.get("pnl") or 0) > 0:
        cell["wins"] += 1


def _spread_breakdown(snap: dict) -> tuple[dict, float | None]:
    stats = {bucket["id"]: _empty_heat_cell() for bucket in SPREAD_BUCKETS}
    spreads: list[float] = []
    for trade in snap.get("trades") or []:
        spread = _trade_spread(trade)
        if spread is None:
            continue
        spreads.append(spread)
        bucket = _bucket_for_spread(spread)
        _accumulate_trade_cell(stats[bucket["id"]], trade)
    _finalize_heat_cells(stats)
    avg_spread = round(sum(spreads) / len(spreads) * 100, 2) if spreads else None
    return stats, avg_spread


def _coin_breakdown(snap: dict) -> dict:
    stats: dict[str, dict] = {}
    for trade in snap.get("trades") or []:
        coin = str(trade.get("coin") or "").upper() or "UNK"
        cell = stats.setdefault(coin, _empty_heat_cell())
        _accumulate_trade_cell(cell, trade)
    _finalize_heat_cells(stats)
    return dict(sorted(stats.items(), key=lambda kv: (-kv[1]["trades"], kv[0])))


def _tte_seconds(trade: dict) -> float | None:
    entry_ts = trade.get("entry_ts")
    expiry = trade.get("expiry")
    if entry_ts is None or not expiry:
        return None
    exp_ts = crypto_arb_bot._expiry_ts(expiry)
    if exp_ts is None:
        return None
    return max(0.0, float(exp_ts) - float(entry_ts))


def _expiry_breakdown(snap: dict) -> dict:
    stats = {bucket["id"]: _empty_heat_cell() for bucket in EXPIRY_BUCKETS}
    for trade in snap.get("trades") or []:
        tte = _tte_seconds(trade)
        if tte is None:
            continue
        bucket = next((
            item for item in EXPIRY_BUCKETS
            if tte >= item["min"]
            and (item["max"] is None or tte < item["max"])
        ), EXPIRY_BUCKETS[-1])
        _accumulate_trade_cell(stats[bucket["id"]], trade)
    _finalize_heat_cells(stats)
    return stats


def _calibration(snap: dict) -> dict:
    buckets = {
        bucket["id"]: {
            "id": bucket["id"],
            "label": bucket["label"],
            "n": 0,
            "predicted_pnl": 0.0,
            "realized_pnl": 0.0,
            "avg_predicted": None,
            "avg_realized": None,
            "avg_error": None,
            "win_rate": None,
            "wins": 0,
        }
        for bucket in SPREAD_BUCKETS
    }
    for trade in snap.get("trades") or []:
        if trade.get("status") != "settled":
            continue
        spread = _trade_spread(trade)
        if spread is None:
            continue
        contracts = int(trade.get("contracts") or 0)
        predicted = float(trade.get("locked_pnl") or 0)
        if not predicted and contracts:
            predicted = spread * contracts
        realized = float(trade.get("pnl") or 0)
        cell = buckets[_bucket_for_spread(spread)["id"]]
        cell["n"] += 1
        cell["predicted_pnl"] += predicted
        cell["realized_pnl"] += realized
        if realized > 0:
            cell["wins"] += 1
    rows = []
    for bucket in SPREAD_BUCKETS:
        cell = buckets[bucket["id"]]
        n = cell["n"]
        if n:
            cell["avg_predicted"] = round(cell["predicted_pnl"] / n, 4)
            cell["avg_realized"] = round(cell["realized_pnl"] / n, 4)
            cell["avg_error"] = round(cell["avg_realized"] - cell["avg_predicted"], 4)
            cell["win_rate"] = round(cell["wins"] / n * 100, 1)
        cell["predicted_pnl"] = round(cell["predicted_pnl"], 2)
        cell["realized_pnl"] = round(cell["realized_pnl"], 2)
        rows.append(cell)
    total_n = sum(row["n"] for row in rows)
    return {
        "buckets": rows,
        "sample_size": total_n,
        "sparse": total_n < 5,
    }


def _funnel_payload(snap: dict) -> dict:
    funnel = dict(snap.get("funnel") or {})
    last = dict(funnel.get("last_scan") or {})
    return {
        "scans": int(funnel.get("scans") or 0),
        "scanned": int(funnel.get("scanned") or 0),
        "eligible": int(funnel.get("eligible") or 0),
        "taken": int(funnel.get("taken") or 0),
        "skipped": int(funnel.get("skipped") or 0),
        "skip_reasons": funnel.get("skip_reasons") or {},
        "take_rate_pct": funnel.get("take_rate_pct"),
        "last_scan": last,
    }


def _sizing_params(meta: dict, snap: dict) -> dict:
    cfg = snap.get("config") or {}
    strategy = str(snap.get("strategy") or meta.get("strategy") or "")
    technical = crypto_arb_bot.STRATEGY_TECHNICAL.get(strategy) or {}
    return {
        "min_edge_cents": round(float(cfg.get("min_edge") or meta.get("min_edge") or 0) * 100, 2),
        "max_bet_pct": round(float(cfg.get("max_bet_pct") or meta.get("max_bet_pct") or 0) * 100, 1),
        "max_exposure_pct": round(
            float(cfg.get("max_exposure_pct") or meta.get("max_exposure_pct") or 0) * 100, 1
        ),
        "max_positions": int(cfg.get("max_positions") or meta.get("max_positions") or 0),
        "max_strike_gap_pct": round(
            float(cfg.get("max_strike_gap") or meta.get("max_strike_gap") or 0) * 100, 3
        ),
        "kelly_frac": cfg.get("kelly_frac", meta.get("kelly_frac")),
        "family": technical.get("family"),
    }


def _recent_trade_timing(snap: dict) -> dict:
    trades = list(snap.get("trades") or [])
    now = time.time()
    latest_entry = None
    latest_settle = None
    for trade in trades:
        entry = trade.get("entry_ts")
        if entry is not None:
            latest_entry = max(latest_entry or 0, float(entry))
        settled = trade.get("settled_at")
        if settled is not None:
            latest_settle = max(latest_settle or 0, float(settled))
    return {
        "last_entry_age_s": int(now - latest_entry) if latest_entry else None,
        "last_settle_age_s": int(now - latest_settle) if latest_settle else None,
    }


def _compact_equity_curve(curve: list | None, limit: int = 90) -> list[dict]:
    points = list(curve or [])
    if len(points) > limit:
        points = points[-limit:]
    return [
        {
            "ts": float(pt.get("ts") or 0),
            "equity": float(pt.get("equity") or 0),
            "life": pt.get("life"),
        }
        for pt in points
        if pt.get("ts") is not None
    ]


def _open_book_entry(bot_id: str, label: str, strategy_id: str, trade: dict) -> dict:
    legs = trade.get("legs") or []
    return {
        "bot_id": bot_id,
        "label": label,
        "strategy": strategy_id,
        "trade_id": trade.get("id"),
        "coin": trade.get("coin"),
        "strike": trade.get("strike"),
        "expiry": trade.get("expiry"),
        "contracts": int(trade.get("contracts") or 0),
        "cost_total": float(trade.get("cost_total") or 0),
        "locked_pnl": float(trade.get("locked_pnl") or 0),
        "spread_cents": trade.get("spread_cents"),
        "settles_in_s": trade.get("settles_in_s"),
        "settlement_status": trade.get("settlement_status"),
        "yes_venue": next((leg.get("venue") for leg in legs if leg.get("side") == "yes"), None),
        "no_venue": next((leg.get("venue") for leg in legs if leg.get("side") == "no"), None),
    }


def _strategy_row(strategy_id: str, meta: dict, snap: dict) -> dict:
    pnl = float(snap.get("confirmed_pnl") or 0)
    equity = float(snap.get("equity") or 0)
    injected = float(snap.get("total_injected") or 50)
    settled = int(snap.get("settled_count") or 0)
    wins = int(snap.get("wins") or 0)
    spread_stats, avg_spread_cents = _spread_breakdown(snap)
    risk = snap.get("risk_metrics") or {}
    timing = _recent_trade_timing(snap)
    return {
        "id": f"crypto-{strategy_id.replace('_', '-')}",
        "type": "crypto_arb",
        "group": "Crypto sizing strategies",
        "strategy": strategy_id,
        "label": meta["label"],
        "desc": meta["desc"],
        "lever": meta["label"],
        "running": True,
        "equity": equity,
        "pnl": pnl,
        "pnl_cents": int(round(pnl * 100)),
        "pending_pnl": float(snap.get("locked_pending") or 0),
        "return_pct": round((pnl / injected * 100) if injected else 0, 2),
        "open_count": int(snap.get("open_count") or 0),
        "trade_count": settled,
        "settled_count": settled,
        "wins": wins,
        "win_rate": round(wins / settled * 100, 1) if settled else None,
        "avg_spread_cents": avg_spread_cents,
        "spread_stats": spread_stats,
        "coin_stats": _coin_breakdown(snap),
        "expiry_stats": _expiry_breakdown(snap),
        "calibration": _calibration(snap),
        "funnel": _funnel_payload(snap),
        "sizing": _sizing_params(meta, snap),
        "last_entry_age_s": timing["last_entry_age_s"],
        "last_settle_age_s": timing["last_settle_age_s"],
        "max_drawdown_pct": risk.get("max_drawdown_pct"),
        "open_exposure_pct": risk.get("open_exposure_pct"),
        "open_concentration_pct": risk.get("open_concentration_pct"),
        "return_std_pct": risk.get("return_std_pct"),
        "equity_curve": _compact_equity_curve(snap.get("equity_curve")),
        "subtitle": f"Life #{snap.get('life', 1)} · $50 bankroll",
        "api": f"/api/cryptoarbitrage/bot/{strategy_id}",
        "config_api": None,
    }


def comparison() -> dict:
    snapshots = crypto_arb_bot.all_strategy_snapshots()
    allocation_history = _record_allocation_history(snapshots)
    rows = [
        _strategy_row(strategy_id, meta, snapshots[strategy_id])
        for strategy_id, meta in crypto_arb_bot.STRATEGIES.items()
    ]
    for row in rows:
        strategy = row["strategy"]
        row["venue_allocation"] = _venue_allocation(
            allocation_history.get(strategy) or [],
            float(snapshots[strategy].get("total_injected") or 50),
        )
    rows.sort(key=lambda row: row["pnl_cents"], reverse=True)
    total_pnl = round(sum(row["pnl"] for row in rows), 2)
    total_equity = round(sum(row["equity"] for row in rows), 2)
    all_trades = [
        trade
        for trades in allocation_history.values()
        for trade in trades
    ]
    open_book = []
    for row in rows:
        snap = snapshots[row["strategy"]]
        for trade in snap.get("trades") or []:
            if trade.get("status") != "open":
                continue
            open_book.append(_open_book_entry(row["id"], row["label"], row["strategy"], trade))
    open_book.sort(key=lambda item: (
        0 if item.get("settlement_status") == "awaiting_price" else 1,
        item.get("settles_in_s") if item.get("settles_in_s") is not None else 10**9,
    ))
    ensemble_funnel = {
        "scans": sum(int((row.get("funnel") or {}).get("scans") or 0) for row in rows),
        "scanned": sum(int((row.get("funnel") or {}).get("scanned") or 0) for row in rows),
        "eligible": sum(int((row.get("funnel") or {}).get("eligible") or 0) for row in rows),
        "taken": sum(int((row.get("funnel") or {}).get("taken") or 0) for row in rows),
        "skipped": sum(int((row.get("funnel") or {}).get("skipped") or 0) for row in rows),
        "skip_reasons": {},
    }
    for row in rows:
        for reason, count in ((row.get("funnel") or {}).get("skip_reasons") or {}).items():
            ensemble_funnel["skip_reasons"][reason] = (
                int(ensemble_funnel["skip_reasons"].get(reason) or 0) + int(count or 0)
            )
    ensemble_funnel["skip_reasons"] = dict(
        sorted(ensemble_funnel["skip_reasons"].items(), key=lambda kv: (-kv[1], kv[0]))
    )
    ensemble_funnel["take_rate_pct"] = (
        round(ensemble_funnel["taken"] / ensemble_funnel["scanned"] * 100, 2)
        if ensemble_funnel["scanned"] else None
    )
    coin_keys = sorted({
        coin
        for row in rows
        for coin in (row.get("coin_stats") or {})
    })
    return {
        "bots": rows,
        "any_running": True,
        "summary": {
            "total_equity": total_equity,
            "total_pnl": total_pnl,
            "running": len(rows),
            "bot_count": len(rows),
            "settled_trades": sum(row["settled_count"] for row in rows),
            "profitable": sum(1 for row in rows if row["pnl"] > 0),
            "open_trades": len(open_book),
            "pending_pnl": round(sum(float(item.get("locked_pnl") or 0) for item in open_book), 2),
            "funnel_taken": ensemble_funnel["taken"],
            "funnel_scanned": ensemble_funnel["scanned"],
        },
        "spread_buckets": [
            {"id": bucket["id"], "label": bucket["label"]}
            for bucket in SPREAD_BUCKETS
        ],
        "expiry_buckets": [
            {"id": bucket["id"], "label": bucket["label"]}
            for bucket in EXPIRY_BUCKETS
        ],
        "coin_keys": coin_keys,
        "funnel": ensemble_funnel,
        "venue_allocation": _venue_allocation(all_trades, total_equity),
        "open_book": open_book,
        "timestamp": time.time(),
    }
