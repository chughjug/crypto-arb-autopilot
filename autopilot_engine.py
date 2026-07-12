"""Autopilot orchestrator — strategy selection, sizing, and trade execution."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import autopilot_executor
import autopilot_store
import crypto_arb
import crypto_arb_bot

log = logging.getLogger(__name__)

_RUNNERS: dict[str, "AutopilotRunner"] = {}
_LOCK = threading.Lock()
_POLL_SECONDS = float(__import__("os").environ.get("AUTOPILOT_POLL_SECONDS", "3"))


def strategy_catalog() -> list[dict]:
    """All strategies with risk/variance metadata for the picker UI."""
    rows = []
    for sid, meta in crypto_arb_bot.STRATEGIES.items():
        tech = crypto_arb_bot.STRATEGY_TECHNICAL.get(sid) or {}
        rows.append({
            "id": sid,
            "label": meta.get("label", sid),
            "desc": meta.get("desc", ""),
            "risk": tech.get("risk", "—"),
            "risk_score": tech.get("risk_score"),
            "variance": tech.get("variance", ""),
            "spread_response": tech.get("spread_response", ""),
            "family": tech.get("family", ""),
            "max_strike_gap_pct": round(float(meta.get("max_strike_gap", 0)) * 100, 3),
            "max_exposure_pct": round(float(meta.get("max_exposure_pct", 0)) * 100, 1),
            "max_bet_pct": round(float(meta.get("max_bet_pct", 0)) * 100, 1) if meta.get("max_bet_pct") else None,
            "min_edge_cents": round(float(meta.get("min_edge", 0)) * 100, 2),
        })
    rows.sort(key=lambda r: (r.get("risk_score") or 99, r["label"]))
    return rows


class AutopilotRunner:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bot: crypto_arb_bot.ArbBot | None = None
        self.stats: dict[str, Any] = {
            "cycles": 0,
            "attempts": 0,
            "fills": 0,
            "last_error": None,
            "last_at": None,
        }

    def _config(self) -> dict:
        return autopilot_store.get_config(self.user_id)

    def _ensure_bot(self, strategy_id: str) -> crypto_arb_bot.ArbBot:
        if self._bot is None or self._bot.cfg.get("strategy") != strategy_id:
            self._bot = crypto_arb_bot.ArbBot(strategy_id)
        return self._bot

    def start(self) -> dict:
        cfg = self._config()
        autopilot_store.save_config(self.user_id, {"running": True})
        with _LOCK:
            if self._thread and self._thread.is_alive():
                return {"running": True, "message": "Already running"}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=f"autopilot-{self.user_id[:8]}"
            )
            _RUNNERS[self.user_id] = self
            self._thread.start()
        autopilot_store.append_log(self.user_id, "info", "Autopilot started", cfg)
        return {"running": True}

    def stop(self) -> dict:
        self._stop.set()
        autopilot_store.save_config(self.user_id, {"running": False})
        autopilot_store.append_log(self.user_id, "info", "Autopilot stopped")
        return {"running": False}

    def _loop(self) -> None:
        crypto_arb.touch()
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                log.exception("autopilot cycle error user=%s", self.user_id)
                self.stats["last_error"] = str(e)
                autopilot_store.append_log(self.user_id, "error", str(e))
            self._stop.wait(_POLL_SECONDS)

    def run_once(self) -> dict:
        cfg = self._config()
        if not cfg.get("running"):
            self._stop.set()
            return {"skipped": "not_running"}
        strategy_id = str(cfg.get("strategy_id") or "half_kelly")
        bot = self._ensure_bot(strategy_id)
        live_mode = bool(cfg.get("live_mode"))
        crypto_arb.touch()

        snap = crypto_arb.snapshot()
        opps = list(snap.get("opportunities") or [])
        opps = crypto_arb_bot._filter_opps_for_strategy(strategy_id, opps)
        opps = autopilot_executor.filter_opportunities(self.user_id, opps)

        self.stats["cycles"] += 1
        self.stats["last_at"] = time.time()

        if not opps:
            return {"opportunities": 0}

        balances = autopilot_executor.venue_balances(self.user_id)
        bankroll_cents = int(float(cfg.get("bankroll_usd") or 300) * 100)

        for opp in opps:
            self.stats["attempts"] += 1
            n = self._size_trade(bot, opp, bankroll_cents, cfg)
            if n < 1:
                continue
            ok_exec, reason = autopilot_executor.can_execute(self.user_id, opp)
            if not ok_exec:
                continue
            if live_mode and not balances.get("ready"):
                continue
            result = autopilot_executor.execute_opportunity(
                self.user_id, opp, contracts=n, live_mode=live_mode
            )
            if result.get("ok"):
                self.stats["fills"] += 1
                return result
            self.stats["last_error"] = "; ".join(result.get("errors") or [])
        return {"opportunities": len(opps), "fills": 0}

    def _size_trade(self, bot: crypto_arb_bot.ArbBot, opp: dict, bankroll_cents: int, cfg: dict) -> int:
        """Mirror ArbBot entry sizing using configured bankroll."""
        strategy = str(bot.cfg.get("strategy") or "half_kelly")
        c = bot.cfg
        yes_cost = float(opp.get("yes_cost") or 0)
        no_cost = float(opp.get("no_cost") or 0)
        if yes_cost <= 0 or no_cost <= 0:
            return 0
        gap = opp.get("strike_gap")
        if gap is None:
            gap = crypto_arb_bot._strike_gap_frac(opp.get("per_venue") or {})
        if gap is None or gap > float(c.get("max_strike_gap") or 0):
            return 0
        leg_cents = round(yes_cost * 100) + round(no_cost * 100)
        net_edge = (100 - leg_cents) / 100.0
        if net_edge < float(c.get("min_edge") or 0):
            return 0
        if not crypto_arb_bot._strategy_allows_entry(strategy, opp, {}, net_edge=net_edge, gap=gap):
            return 0
        exp_ts = crypto_arb_bot._expiry_ts(opp.get("expiry"))
        now = time.time()
        reserve_pct = float(cfg.get("reserve_pct") or 30) / 100.0
        deployable = int(bankroll_cents * (1.0 - reserve_pct))
        max_exp = float(c.get("max_exposure_pct") or 0.6)
        deployed = 0
        equity = deployable
        cash = deployable
        return crypto_arb_bot._strategy_contracts(
            strategy, net_edge, leg_cents, cash, deployed, equity, c,
            exp_ts or now + 3600, now, gap,
        )

    def status(self) -> dict:
        cfg = self._config()
        return {
            "config": cfg,
            "stats": dict(self.stats),
            "venues": autopilot_store.all_venue_status(self.user_id),
            "balances": autopilot_executor.venue_balances(self.user_id),
            "thread_alive": bool(self._thread and self._thread.is_alive()),
        }


def get_runner(user_id: str) -> AutopilotRunner:
    with _LOCK:
        if user_id not in _RUNNERS:
            _RUNNERS[user_id] = AutopilotRunner(user_id)
        return _RUNNERS[user_id]


def resume_all() -> None:
    """Restart runners marked running (e.g. after server boot)."""
    # Scan would need list_users_with_running - skip for MVP; user restarts manually
    pass
