"""/cryptoarbitrage — always-on paper-trading arbitrage bot.

Always running, never stops.  Uses half-Kelly position sizing with a $50 bankroll.
If the bankroll busts (cash too low for a minimum trade), it logs the bust, injects
another $50, and keeps going.  The UI shows lifetime stats across all "lives."

Sizing — modified half-Kelly for locked arbs
  ε  = assumed execution failure rate (2 %)
  L  = loss fraction on failure (50 %)
  e  = net edge per $1
  f* = [(1−ε)·e − ε·L] / (e·L)       full Kelly fraction
  Bet = min(f*/2, 5 % of bankroll)     half-Kelly, capped
  Max total deployed ≤ 60 % of bankroll

Bust rule
  When available cash < $1 (can't buy even 1 contract) AND no open positions,
  the life is over → log bust, add $50, increment life counter, continue.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import crypto_arb
import bot_persistence

# ------------------------------------------------------------------ tunables
RELOAD_AMOUNT = 50.0           # dollars injected on each bust / initial load
BUST_THRESHOLD_CENTS = 100     # cash below $1 with 0 open = bust

DEFAULTS = {
    "poll_interval": 2.0,
    "starting_balance": RELOAD_AMOUNT,
    "strategy": "half_kelly",
    "min_edge": 0.01,
    "max_strike_gap": 0.005,
    "fee_cents": 0.0,
    "max_positions": 25,
    "flat_contracts": 2,
    "spread_ref": 0.05,
    # Kelly parameters
    "epsilon": 0.02,
    "loss_frac": 0.50,
    "kelly_frac": 0.50,
    "max_bet_pct": 0.05,
    "max_exposure_pct": 0.60,
}

# Paper-trading sizing / entry strategies (strategy id → defaults + label for UI)
STRATEGIES: dict[str, dict] = {
    "half_kelly": {
        "label": "Half Kelly",
        "desc": "Default — fractional Kelly capped at 5% per trade, 60% max deployed.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.05,
        "max_exposure_pct": 0.60,
        "min_edge": 0.01,
        "max_strike_gap": 0.005,
        "max_positions": 25,
    },
    "full_kelly": {
        "label": "Full Kelly",
        "desc": "Full Kelly fraction — aggressive growth, higher bust risk.",
        "kelly_frac": 1.0,
        "max_bet_pct": 0.25,
        "max_exposure_pct": 0.80,
        "min_edge": 0.01,
        "max_strike_gap": 0.006,
        "max_positions": 15,
    },
    "yolo": {
        "label": "YOLO",
        "desc": "All available cash on the single best arb each scan.",
        "min_edge": 0.01,
        "max_strike_gap": 0.01,
        "max_positions": 1,
        "max_exposure_pct": 1.0,
    },
    "flat_unit": {
        "label": "Flat unit",
        "desc": "Fixed contract count every trade (default 2), ignores edge size.",
        "flat_contracts": 2,
        "min_edge": 0.01,
        "max_strike_gap": 0.003,
        "max_positions": 20,
        "max_exposure_pct": 0.80,
    },
    "edge_tiered": {
        "label": "Edge tiered",
        "desc": "Bet 10% / 5% / 2% of bankroll when edge ≥ $0.10 / $0.05 / $0.01.",
        "min_edge": 0.01,
        "max_strike_gap": 0.003,
        "max_positions": 20,
        "max_exposure_pct": 0.70,
    },
    "wide_arb_allin": {
        "label": "Wide arb all-in",
        "desc": "Only trade edge ≥ $0.05, then deploy up to 90% of bankroll.",
        "min_edge": 0.05,
        "max_bet_pct": 0.90,
        "max_exposure_pct": 0.95,
        "max_strike_gap": 0.004,
        "max_positions": 3,
    },
    "max_diversify": {
        "label": "Max diversify",
        "desc": "Tiny 1.5% bets, up to 25 concurrent positions.",
        "min_edge": 0.008,
        "max_bet_pct": 0.015,
        "max_exposure_pct": 0.90,
        "max_strike_gap": 0.01,
        "max_positions": 25,
    },
    "time_urgency": {
        "label": "Time urgency",
        "desc": "Half-Kelly base sizing, 2× in last 60s before expiry.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.10,
        "max_exposure_pct": 0.70,
        "min_edge": 0.01,
        "max_strike_gap": 0.005,
        "max_positions": 15,
    },
    "venue_pair": {
        "label": "Venue pair filter",
        "desc": "Only Kalshi↔Polymarket or crypto.com↔Kalshi pairs.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.05,
        "max_exposure_pct": 0.60,
        "min_edge": 0.01,
        "max_strike_gap": 0.005,
        "max_positions": 20,
    },
    "compound": {
        "label": "Compound",
        "desc": "Size off total equity (cash + open payout), reinvest everything.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.08,
        "max_exposure_pct": 0.95,
        "min_edge": 0.01,
        "max_strike_gap": 0.006,
        "max_positions": 12,
    },
    "sniper": {
        "label": "Sniper",
        "desc": "One position per coin per expiry — first qualifying arb only.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.06,
        "max_exposure_pct": 0.50,
        "min_edge": 0.01,
        "max_strike_gap": 0.0005,
        "max_positions": 30,
    },
    # --- spread / price-difference based ---
    "spread_linear": {
        "label": "Spread linear",
        "desc": "Bet size grows linearly with arb edge — 2% bankroll per 1¢ spread.",
        "min_edge": 0.005,
        "max_bet_pct": 0.20,
        "max_exposure_pct": 0.75,
        "max_strike_gap": 0.007,
        "max_positions": 18,
    },
    "spread_proportional": {
        "label": "Spread proportional",
        "desc": "Deploy base % at 5¢ edge; scales up/down with spread width.",
        "min_edge": 0.005,
        "max_bet_pct": 0.06,
        "spread_ref": 0.05,
        "max_exposure_pct": 0.70,
        "max_strike_gap": 0.007,
        "max_positions": 20,
    },
    "edge_squared": {
        "label": "Edge squared",
        "desc": "Size ∝ edge² — fat spreads get disproportionately large bets.",
        "min_edge": 0.01,
        "max_bet_pct": 0.25,
        "max_exposure_pct": 0.80,
        "max_strike_gap": 0.007,
        "max_positions": 12,
    },
    "spread_escalator": {
        "label": "Spread escalator",
        "desc": "5-tier ladder: 1.5% → 15% bankroll as spread widens from 1¢ to 10¢+.",
        "min_edge": 0.01,
        "max_exposure_pct": 0.85,
        "max_strike_gap": 0.007,
        "max_positions": 15,
    },
    "micro_spread": {
        "label": "Micro spread",
        "desc": "Only thin 0.5¢–2¢ arbs — many small, reliable locks.",
        "min_edge": 0.005,
        "max_bet_pct": 0.025,
        "max_exposure_pct": 0.65,
        "max_strike_gap": 0.0005,
        "max_positions": 25,
    },
    "fat_spread": {
        "label": "Fat spread",
        "desc": "Only trade when spread ≥ 8¢, then deploy up to 12% bankroll.",
        "min_edge": 0.08,
        "max_bet_pct": 0.12,
        "max_exposure_pct": 0.70,
        "max_strike_gap": 0.004,
        "max_positions": 8,
    },
    "bimodal_spread": {
        "label": "Bimodal spread",
        "desc": "Skip middling arbs — only thin (<2¢) or fat (≥6¢) spreads.",
        "min_edge": 0.005,
        "max_bet_pct": 0.07,
        "max_exposure_pct": 0.75,
        "max_strike_gap": 0.006,
        "max_positions": 18,
    },
    "spread_top3": {
        "label": "Top 3 spreads",
        "desc": "Each scan, only enter the 3 widest-spread opportunities.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.06,
        "max_exposure_pct": 0.65,
        "min_edge": 0.01,
        "max_strike_gap": 0.005,
        "max_positions": 3,
    },
    "best_spread_only": {
        "label": "Best spread only",
        "desc": "One entry per scan — the single widest arb edge available.",
        "kelly_frac": 0.75,
        "max_bet_pct": 0.15,
        "max_exposure_pct": 0.80,
        "min_edge": 0.01,
        "max_strike_gap": 0.005,
        "max_positions": 1,
    },
    "gap_tight_only": {
        "label": "Tight strike gap",
        "desc": "Only when venue strikes agree within 0.03%; standard Kelly sizing.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.06,
        "max_exposure_pct": 0.65,
        "min_edge": 0.008,
        "max_strike_gap": 0.0003,
        "max_positions": 20,
    },
    "gap_inverse": {
        "label": "Gap inverse size",
        "desc": "Kelly sizing scaled by strike tightness — tighter gap → bigger bet.",
        "kelly_frac": 0.50,
        "max_bet_pct": 0.08,
        "max_exposure_pct": 0.70,
        "min_edge": 0.01,
        "max_strike_gap": 0.008,
        "max_positions": 18,
    },
}

# Technical descriptions are intentionally separate from tunable numeric defaults.
# "Variance" refers to bankroll/execution variance, not the idealized binary payout:
# an exactly matched, fully filled complementary pair has deterministic gross payout.
STRATEGY_TECHNICAL: dict[str, dict] = {
    "half_kelly": {
        "family": "Fractional log-growth", "risk": "Moderate", "risk_score": 4,
        "variance": "Moderate; approximately one quarter of full-Kelly sizing variance before caps.",
        "formula": "f = 0.5 · max(0, ((1−ε)e−εL)/(eL)); wager = min(fB, 0.05B, exposure room).",
        "spread_response": "Nonlinear Kelly response to net edge e, then hard-capped at 5% of cash.",
        "failure_mode": "Model error in ε or L can oversize thin edges; correlated expiries defeat the diversification assumption.",
    },
    "full_kelly": {
        "family": "Maximum log-growth", "risk": "Very high", "risk_score": 9,
        "variance": "Highest Kelly-path variance and deepest expected drawdowns among model-based strategies.",
        "formula": "f = max(0, ((1−ε)e−εL)/(eL)); wager = min(fB, 0.25B, exposure room).",
        "spread_response": "Aggressively increases size as e clears the model-error break-even threshold.",
        "failure_mode": "Kelly is optimal only for calibrated independent outcomes; small edge overstatement causes severe overbetting.",
    },
    "yolo": {
        "family": "Winner-take-all concentration", "risk": "Extreme", "risk_score": 10,
        "variance": "Maximum concentration variance: one fill, venue, oracle, or settlement error dominates the account.",
        "formula": "n = floor(available cash / paired contract cost), applied only to max(e) each scan.",
        "spread_response": "Ranks by e but ignores its magnitude after selection; 1¢ and 20¢ edges both receive all cash.",
        "failure_mode": "No diversification or sizing margin for slippage, partial fills, fees, cancellation, or source mismatch.",
    },
    "flat_unit": {
        "family": "Constant notional", "risk": "Low", "risk_score": 3,
        "variance": "Low per event; aggregate variance rises linearly with trade count and cross-market correlation.",
        "formula": "n = min(2, affordable contracts, exposure-room contracts), independent of e.",
        "spread_response": "Entry threshold filters spreads; accepted spreads receive identical contract count.",
        "failure_mode": "Underallocates unusually strong edges and overallocates marginal edges relative to their expected value.",
    },
    "edge_tiered": {
        "family": "Discrete edge ladder", "risk": "Moderate", "risk_score": 5,
        "variance": "Piecewise-constant variance with discontinuous jumps at 5¢ and 10¢.",
        "formula": "w/B = 2% for 1–5¢, 5% for 5–10¢, 10% for ≥10¢ net edge.",
        "spread_response": "Monotone step function; robust to tiny quote noise except at tier boundaries.",
        "failure_mode": "A one-tick edge change at a boundary can more than double size despite nearly identical EV.",
    },
    "wide_arb_allin": {
        "family": "High-edge concentration", "risk": "Extreme", "risk_score": 10,
        "variance": "Very high concentration and liquidity variance; at most three positions can absorb 95% exposure.",
        "formula": "Enter only e ≥ 5¢; wager = min(90%B, remaining 95% exposure room).",
        "spread_response": "Binary response: zero below 5¢, near-all-in above 5¢.",
        "failure_mode": "Observed wide spreads often proxy stale quotes, low depth, or legging risk rather than free EV.",
    },
    "max_diversify": {
        "family": "Broad equal-risk allocation", "risk": "Low", "risk_score": 3,
        "variance": "Low idiosyncratic variance; residual variance remains high when positions share coin, expiry, or venue.",
        "formula": "wager = 1.5%B per accepted pair, up to 25 positions and 90% aggregate exposure.",
        "spread_response": "Spread affects admission only; size is constant after e ≥ 0.8¢.",
        "failure_mode": "Nominal diversification can be illusory because many contracts resolve from the same underlying price print.",
    },
    "time_urgency": {
        "family": "Expiry-weighted Kelly", "risk": "High", "risk_score": 7,
        "variance": "Higher near-expiry notional variance; shorter holding time does not remove oracle or fill risk.",
        "formula": "Half-Kelly multiplier m(T): 1.0 if T>180s, 1.5 if 60<T≤180s, 2.0 if T≤60s.",
        "spread_response": "Kelly responds to e; time-to-expiry independently scales the resulting fraction.",
        "failure_mode": "Late quotes may be stale and books may thin near expiry, so urgency can amplify adverse execution.",
    },
    "venue_pair": {
        "family": "Counterparty/venue filter", "risk": "Moderate", "risk_score": 5,
        "variance": "Moderate sizing variance with concentrated operational exposure to approved venue pairs.",
        "formula": "Half-Kelly only when the selected YES/NO venues are in the approved cross-venue set.",
        "spread_response": "Kelly responds to e after venue eligibility; size scales down as strike gap widens.",
        "failure_mode": "Venue filtering does not eliminate differing oracle sources, cancellation rules, or withdrawal risk.",
    },
    "compound": {
        "family": "Equity-reinvestment Kelly", "risk": "Very high", "risk_score": 8,
        "variance": "Path-dependent variance; open projected payouts recursively increase subsequent sizing.",
        "formula": "Half-Kelly on B = cash + projected open payout, capped at 8% per pair and 95% exposure.",
        "spread_response": "Kelly response to e with a growing equity base, producing positive feedback after gains.",
        "failure_mode": "Treating unsettled payout as bankroll can pyramid correlated exposure and magnify a common settlement failure.",
    },
    "sniper": {
        "family": "Coin-expiry deduplication", "risk": "Low", "risk_score": 3,
        "variance": "Lower cluster variance because each coin-expiry contributes at most one position.",
        "formula": "Half-Kelly, rejecting any new pair whose (coin, exact expiry) already exists.",
        "spread_response": "Sizes by e but is arrival-order sensitive: the first qualifying quote consumes the slot.",
        "failure_mode": "Can lock in a thin early spread and miss a materially better quote later in the same window.",
    },
    "spread_linear": {
        "family": "Linear edge elasticity", "risk": "High", "risk_score": 7,
        "variance": "Variance grows roughly with e² because notional grows linearly with e.",
        "formula": "w/B = min(20%, 2e); e.g. 1¢→2%, 5¢→10%, 10¢→20%.",
        "spread_response": "Constant sizing elasticity until the 20% cap; directly maps one spread cent to two bankroll points.",
        "failure_mode": "If wide spreads are caused by stale or shallow quotes, size increases exactly where execution error is largest.",
    },
    "spread_proportional": {
        "family": "Reference-normalized edge", "risk": "Moderate", "risk_score": 6,
        "variance": "Smooth moderate variance, bounded at 15% after normalization.",
        "formula": "w/B = clamp(6% · e/5¢, 0.5%, 15%).",
        "spread_response": "Unit elasticity around the 5¢ reference spread with explicit floor and ceiling.",
        "failure_mode": "The 5¢ reference is heuristic and does not adapt to coin liquidity, fees, or venue-specific fill probability.",
    },
    "edge_squared": {
        "family": "Convex edge concentration", "risk": "Very high", "risk_score": 9,
        "variance": "Strongly convex: notional variance scales approximately with e⁴ before the 25% cap.",
        "formula": "w/B = clamp(25% · (e/5¢)², 0.5%, 25%).",
        "spread_response": "Superlinear; doubling e quadruples target allocation until capped.",
        "failure_mode": "Extremely sensitive to spread-estimation error and systematically concentrates in anomalous quotes.",
    },
    "spread_escalator": {
        "family": "Five-level spread ladder", "risk": "Moderate-high", "risk_score": 6,
        "variance": "Controlled within tiers, with jump risk at 2¢, 4¢, 7¢, and 10¢.",
        "formula": "w/B = 1.5%, 3%, 6%, 10%, 15% across increasing spread tiers.",
        "spread_response": "Monotone staircase balancing interpretability against boundary discontinuity.",
        "failure_mode": "Quote flicker around tier edges can materially change size without a meaningful EV change.",
    },
    "micro_spread": {
        "family": "Thin-edge harvesting", "risk": "Moderate", "risk_score": 5,
        "variance": "Low modeled payout variance but high sensitivity to one-cent fees, rounding, and execution leakage.",
        "formula": "Accept 0.5¢ ≤ e < 2¢; wager = 2.5%B per pair.",
        "spread_response": "Hard band-pass filter; no size differentiation inside the micro-spread interval.",
        "failure_mode": "Small gross EV is easily erased by fees, partial fills, latency, and settlement-source basis.",
    },
    "fat_spread": {
        "family": "Tail-edge selection", "risk": "High", "risk_score": 7,
        "variance": "Low trade frequency but high per-trade model and execution variance.",
        "formula": "Accept e ≥ 8¢; wager = 12%B, capped at eight positions and 70% exposure.",
        "spread_response": "Threshold-only response: every qualifying fat spread receives the same percentage.",
        "failure_mode": "Selection is biased toward broken, stale, low-depth, or semantically mismatched markets.",
    },
    "bimodal_spread": {
        "family": "Barbell edge selection", "risk": "High", "risk_score": 7,
        "variance": "Mixture variance: many small 3% bets plus sparse 7% tail-edge bets.",
        "formula": "Accept e<2¢ at 3%B or e≥6¢ at 7%B; reject the 2–6¢ middle.",
        "spread_response": "Discontinuous barbell intended to compare scalable micro edges against rare large edges.",
        "failure_mode": "The omitted middle has no theoretical disadvantage; cutoffs can create selection bias.",
    },
    "spread_top3": {
        "family": "Cross-sectional rank selection", "risk": "Moderate", "risk_score": 6,
        "variance": "Concentrated in three contemporaneous quotes, often sharing the same market regime.",
        "formula": "Rank all eligible pairs by e; apply Half-Kelly only to ranks 1–3.",
        "spread_response": "Relative rather than absolute: size follows Kelly, admission depends on rank.",
        "failure_mode": "Top-ranked spreads may be stale; rankings are unstable when edges differ by only a tick.",
    },
    "best_spread_only": {
        "family": "Single-rank concentration", "risk": "Very high", "risk_score": 8,
        "variance": "High concentration, though capped below YOLO; only one quote drives each scan.",
        "formula": "Choose argmax(e), then size at 0.75-Kelly with a 15%B cap.",
        "spread_response": "Uses spread for both ranking and Kelly sizing, creating double concentration in max(e).",
        "failure_mode": "Winner's-curse exposure: the largest observed edge is also most likely to contain measurement error.",
    },
    "gap_tight_only": {
        "family": "Tight strike-gap gate", "risk": "Moderate", "risk_score": 4,
        "variance": "Lower than broad strategies because ΔK≤0.03% filters out most oracle-mismatch risk.",
        "formula": "Admit only ΔK≤0.03%, then Half-Kelly with gap-weighted sizing.",
        "spread_response": "Sizes by e; only the tightest cross-venue strike clusters pass admission.",
        "failure_mode": "Small residual gaps still do not prove identical oracle, timestamp, or rounding sources.",
    },
    "gap_inverse": {
        "family": "Strike-gap-weighted Kelly", "risk": "Moderate", "risk_score": 4,
        "variance": "Inversely scales with ΔK — tight gaps size up, wide gaps shrink toward 25% of Kelly.",
        "formula": "Multiplier 0.25+0.75(1−ΔK/ΔKmax) on Half-Kelly; ΔKmax=0.8%.",
        "spread_response": "Kelly responds to e; strike tightness independently scales the resulting fraction.",
        "failure_mode": "Wide admitted gaps create an uncovered interval with loss term n·P(Kno≤S<Kyes).",
    },
}

_VENUE_PAIR_ALLOWED = frozenset({
    frozenset({"kalshi", "polymarket"}),
    frozenset({"cryptocom", "kalshi"}),
    frozenset({"cryptocom", "polymarket"}),
})

LOG_LIMIT = 500


def _expiry_ts(expiry: str) -> float | None:
    if not expiry:
        return None
    try:
        return datetime.fromisoformat(expiry + "+00:00").timestamp()
    except ValueError:
        return None


def _strike_gap_frac(per_venue: dict) -> float | None:
    strikes = [pv.get("strike") for pv in per_venue.values() if pv.get("strike")]
    strikes = [s for s in strikes if s]
    if len(strikes) < 2:
        return 0.0 if strikes else None
    lo, hi = min(strikes), max(strikes)
    mean = sum(strikes) / len(strikes)
    return (hi - lo) / mean if mean else 0.0


def _gap_wager_mult(gap: float | None, cfg: dict, *, floor: float = 0.35) -> float:
    """Tighter venue strike agreement → larger wager (per-strategy max_strike_gap)."""
    max_gap = float(cfg.get("max_strike_gap") or 0)
    if max_gap <= 0 or gap is None:
        return 1.0
    tight = 1.0 - min(1.0, gap / max_gap)
    return floor + (1.0 - floor) * tight


def _venue_info(detail: dict | None) -> dict:
    if not detail:
        return {}
    out = {
        "strike": detail.get("strike"),
        "yes": detail.get("yes"),
        "no": detail.get("no"),
        "strike_verified": detail.get("strike_verified") is True,
        "strike_source": detail.get("strike_source"),
        "strike_evidence": detail.get("strike_evidence"),
        "strike_timestamp_ms": detail.get("strike_timestamp_ms"),
        "strike_delay_ms": detail.get("strike_delay_ms"),
        "settlement_rule_verified": detail.get("settlement_rule_verified") is True,
        "yes_operator": detail.get("yes_operator"),
        "no_operator": detail.get("no_operator"),
        "rule_evidence": detail.get("rule_evidence"),
    }
    if detail.get("slug"):
        out["slug"] = detail["slug"]
    if detail.get("ticker"):
        out["ticker"] = detail["ticker"]
    return out


def _leg_pnl(cost_per: float, contracts: int, won: bool) -> dict:
    cost_total = round(cost_per * contracts, 2)
    payout = float(contracts) if won else 0.0
    pnl = round(payout - cost_total, 2)
    return {
        "cost_per": cost_per,
        "contracts": contracts,
        "cost_total": cost_total,
        "payout": round(payout, 2),
        "pnl": pnl,
        "won": won,
    }


def _threshold_won(price: float, strike: float, operator: str) -> bool:
    return crypto_arb.threshold_won(price, strike, operator)


def _open_leg(side: str, venue: str, cost_per: float, contracts: int,
              strike: float | None, venue_detail: dict | None) -> dict:
    cost_total = round(cost_per * contracts, 2)
    win = _leg_pnl(cost_per, contracts, True)
    lose = _leg_pnl(cost_per, contracts, False)
    return {
        "side": side,
        "venue": venue,
        "strike": strike,
        "venue_detail": _venue_info(venue_detail),
        "cost_per": cost_per,
        "contracts": contracts,
        "cost_total": cost_total,
        "pnl_if_win": win["pnl"],
        "pnl_if_lose": lose["pnl"],
    }


def _settled_leg(side: str, venue: str, cost_per: float, contracts: int,
                 strike: float | None, won: bool, venue_detail: dict | None) -> dict:
    row = _leg_pnl(cost_per, contracts, won)
    return {
        "side": side,
        "venue": venue,
        "strike": strike,
        "venue_detail": _venue_info(venue_detail),
        **row,
    }


def _trade_from_open(pos: dict, now: float) -> dict:
    vd = pos.get("venue_details") or {}
    yes_v, no_v = pos["yes_venue"], pos["no_venue"]
    yes_strike = (vd.get(yes_v) or {}).get("strike") or pos["strike"]
    no_strike = (vd.get(no_v) or {}).get("strike") or pos["strike"]
    n = pos["contracts"]
    yes_leg = _open_leg("yes", yes_v, pos["yes_cost"], n, yes_strike, vd.get(yes_v))
    no_leg = _open_leg("no", no_v, pos["no_cost"], n, no_strike, vd.get(no_v))
    return {
        "id": pos.get("key") or f"{pos['coin']}|{pos['expiry']}|{pos['strike']}",
        "status": "open",
        "life": None,
        "coin": pos["coin"],
        "strike": pos["strike"],
        "expiry": pos["expiry"],
        "contracts": n,
        "cost_total": round(pos["cost_cents"] / 100.0, 2),
        "locked_pnl": round(pos["locked_cents"] / 100.0, 2),
        "spread": pos.get("spread"),
        "spread_cents": round(float(pos.get("spread") or 0) * 100, 2),
        "strike_gap_pct": round(float(pos.get("gap") or 0) * 100, 4),
        "pnl": None,
        "spot_price": None,
        "winning_leg": None,
        "outcome": None,
        "settles_in_s": max(0, int(pos["expiry_ts"] - now)),
        "settlement_status": "awaiting_price" if now >= pos["expiry_ts"] else "pending_expiry",
        "confirmed": False,
        "settled_at": None,
        "entry_ts": pos.get("entry_ts"),
        "strategy": pos.get("strategy"),
        "legs": [yes_leg, no_leg],
    }


def _trade_from_settled(p: dict) -> dict:
    vd = p.get("venue_details") or {}
    yes_v, no_v = p["yes_venue"], p["no_venue"]
    yes_strike = p.get("yes_strike") or (vd.get(yes_v) or {}).get("strike") or p["strike"]
    no_strike = p.get("no_strike") or (vd.get(no_v) or {}).get("strike") or p["strike"]
    n = p["contracts"]
    yes_won = bool(p.get("yes_won"))
    no_won = bool(p.get("no_won"))
    yes_leg = _settled_leg("yes", yes_v, p["yes_cost"], n, yes_strike, yes_won, vd.get(yes_v))
    no_leg = _settled_leg("no", no_v, p["no_cost"], n, no_strike, no_won, vd.get(no_v))
    if p.get("yes_pnl") is not None:
        yes_leg["pnl"] = round(float(p["yes_pnl"]), 2)
    if p.get("no_pnl") is not None:
        no_leg["pnl"] = round(float(p["no_pnl"]), 2)
    return {
        "id": f"{p['coin']}|{p['expiry']}|{p['strike']}|{p.get('settled_at')}",
        "status": "settled",
        "life": p.get("life"),
        "coin": p["coin"],
        "strike": p["strike"],
        "expiry": p["expiry"],
        "contracts": n,
        "cost_total": round(float(p.get("cost") or 0), 2),
        "locked_pnl": None,
        "spread": p.get("spread"),
        "spread_cents": round(float(p.get("spread") or 0) * 100, 2),
        "strike_gap_pct": round(float(p.get("gap") or 0) * 100, 4),
        "pnl": round(float(p.get("pnl") or 0), 2),
        "spot_price": p.get("spot_price"),
        "winning_leg": p.get("winning_leg"),
        "outcome": p.get("outcome"),
        "settles_in_s": None,
        "settled_at": p.get("settled_at"),
        "settlement_status": "confirmed",
        "confirmed": True,
        "entry_ts": p.get("entry_ts"),
        "strategy": p.get("strategy"),
        "legs": [yes_leg, no_leg],
    }


def _leg_pnls_from_settle(contracts: int, yes_cost: float, no_cost: float,
                          yes_won: bool, no_won: bool) -> tuple[float, float]:
    yes = _leg_pnl(yes_cost, contracts, yes_won)["pnl"]
    no = _leg_pnl(no_cost, contracts, no_won)["pnl"]
    return yes, no


def _risk_metrics(settled: list[dict], open_trades: list[dict],
                  equity_curve: list[dict], equity: float, deployed: float) -> dict:
    returns = [
        float(trade["pnl"]) / float(trade["cost_total"])
        for trade in settled
        if float(trade.get("cost_total") or 0) > 0
    ]
    mean = sum(returns) / len(returns) if returns else None
    variance = (
        sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if mean is not None and len(returns) > 1 else None
    )
    downside = (
        math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
        if returns else None
    )
    peak = 0.0
    max_drawdown = 0.0
    for point in equity_curve:
        value = float(point.get("equity") or 0)
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    open_costs = [float(trade.get("cost_total") or 0) for trade in open_trades]
    gross_wins = sum(max(float(trade.get("pnl") or 0), 0.0) for trade in settled)
    gross_losses = abs(sum(min(float(trade.get("pnl") or 0), 0.0) for trade in settled))
    all_trades = list(settled) + list(open_trades)
    gaps = [
        float(trade.get("strike_gap_pct") or 0) / 100.0
        for trade in all_trades
        if trade.get("strike_gap_pct") is not None
    ]
    spreads = [float(trade.get("spread_cents") or 0) for trade in all_trades if trade.get("spread_cents") is not None]
    avg_gap_pct = round(sum(gaps) / len(gaps) * 100, 4) if gaps else None
    avg_spread_cents = round(sum(spreads) / len(spreads), 2) if spreads else None
    gap_variance_mult = (
        round(1.0 + (avg_gap_pct / 100.0) * 8.0, 4)
        if avg_gap_pct is not None and variance is not None else None
    )
    return {
        "sample_size": len(returns),
        "mean_return_pct": round(mean * 100, 3) if mean is not None else None,
        "return_variance": round(variance, 8) if variance is not None else None,
        "return_std_pct": round(math.sqrt(variance) * 100, 3) if variance is not None else None,
        "gap_adjusted_variance": (
            round(variance * gap_variance_mult, 8)
            if variance is not None and gap_variance_mult is not None else None
        ),
        "avg_strike_gap_pct": avg_gap_pct,
        "avg_spread_cents": avg_spread_cents,
        "downside_deviation_pct": round(downside * 100, 3) if downside is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 3),
        "open_exposure_pct": round(deployed / equity * 100, 2) if equity > 0 else None,
        "open_concentration_pct": (
            round(max(open_costs) / deployed * 100, 2) if open_costs and deployed > 0 else None
        ),
        "profit_factor": round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
    }


def _kelly_contracts(edge: float, cost_per_contract_cents: int,
                     bankroll_cents: int, deployed_cents: int, cfg: dict,
                     wager_mult: float = 1.0) -> int:
    """Kelly position sizing: contracts from bankroll (cash or equity)."""
    eps = float(cfg["epsilon"])
    L = float(cfg["loss_frac"])
    kf = float(cfg["kelly_frac"])
    max_bet_pct = float(cfg["max_bet_pct"])
    max_exp_pct = float(cfg["max_exposure_pct"])

    if edge <= 0 or cost_per_contract_cents <= 0 or bankroll_cents <= 0:
        return 0

    denom = edge * L
    if denom <= 0:
        return 0
    f_star = ((1 - eps) * edge - eps * L) / denom
    if f_star <= 0:
        return 0

    f_use = f_star * kf * wager_mult
    bankroll_dollars = bankroll_cents / 100.0
    max_bet_dollars = bankroll_dollars * max_bet_pct
    wager_dollars = min(f_use * bankroll_dollars, max_bet_dollars)

    max_deploy = bankroll_dollars * max_exp_pct
    already_deployed = deployed_cents / 100.0
    room = max(0, max_deploy - already_deployed)
    wager_dollars = min(wager_dollars, room)

    cost_per_contract_dollars = cost_per_contract_cents / 100.0
    n = int(wager_dollars / cost_per_contract_dollars)
    return max(n, 0)


def _pct_contracts(pct: float, cost_per_contract_cents: int,
                     bankroll_cents: int, deployed_cents: int, cfg: dict) -> int:
    if cost_per_contract_cents <= 0 or bankroll_cents <= 0:
        return 0
    bankroll = bankroll_cents / 100.0
    max_exp = float(cfg["max_exposure_pct"])
    room = max(0, bankroll * max_exp - deployed_cents / 100.0)
    wager = min(bankroll * pct, room)
    return max(0, int(wager / (cost_per_contract_cents / 100.0)))


def _strategy_contracts(strategy: str, net_edge: float, leg_cents: int,
                        cash_cents: int, deployed_cents: int, equity_cents: int,
                        cfg: dict, exp_ts: float, now: float,
                        gap: float | None = None) -> int:
    bankroll = equity_cents if strategy == "compound" else cash_cents

    if strategy == "yolo":
        room = cash_cents
        if float(cfg["max_exposure_pct"]) < 1.0:
            max_deploy = int(bankroll * float(cfg["max_exposure_pct"]))
            room = min(room, max(0, max_deploy - deployed_cents))
        return max(0, int(room / leg_cents)) if leg_cents > 0 else 0

    if strategy == "flat_unit":
        n = int(cfg.get("flat_contracts", 2))
        afford = int(cash_cents / leg_cents) if leg_cents > 0 else 0
        max_exp = float(cfg["max_exposure_pct"])
        room = max(0, bankroll / 100.0 * max_exp - deployed_cents / 100.0)
        cap = int(room / (leg_cents / 100.0)) if leg_cents > 0 else 0
        return max(0, min(n, afford, cap))

    if strategy == "edge_tiered":
        if net_edge >= 0.10:
            pct = 0.10
        elif net_edge >= 0.05:
            pct = 0.05
        else:
            pct = 0.02
        return _pct_contracts(pct, leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "wide_arb_allin":
        if net_edge < 0.05:
            return 0
        return _pct_contracts(float(cfg["max_bet_pct"]), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "max_diversify":
        return _pct_contracts(float(cfg["max_bet_pct"]), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "time_urgency":
        secs = max(0, exp_ts - now)
        mult = 2.0 if secs <= 60 else (1.5 if secs <= 180 else 1.0)
        mult *= _gap_wager_mult(gap, cfg)
        return _kelly_contracts(net_edge, leg_cents, bankroll, deployed_cents, cfg, mult)

    if strategy == "spread_linear":
        # 2% of bankroll per 1¢ edge (e.g. 3¢ → 6%), capped; tighter ΔK → larger
        pct = min(float(cfg["max_bet_pct"]), net_edge * 2.0)
        pct *= _gap_wager_mult(gap, cfg, floor=0.5)
        return _pct_contracts(pct, leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "spread_proportional":
        ref = float(cfg.get("spread_ref", 0.05))
        base = float(cfg["max_bet_pct"])
        pct = min(base * 2.5, base * (net_edge / ref)) if ref > 0 else base
        pct *= _gap_wager_mult(gap, cfg, floor=0.5)
        return _pct_contracts(max(pct, 0.005), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "edge_squared":
        ref = 0.05
        base = float(cfg["max_bet_pct"])
        ratio = (net_edge / ref) if ref > 0 else 0.0
        pct = min(base, base * ratio * ratio)
        pct *= _gap_wager_mult(gap, cfg, floor=0.45)
        return _pct_contracts(max(pct, 0.005), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "spread_escalator":
        if net_edge >= 0.10:
            pct = 0.15
        elif net_edge >= 0.07:
            pct = 0.10
        elif net_edge >= 0.04:
            pct = 0.06
        elif net_edge >= 0.02:
            pct = 0.03
        else:
            pct = 0.015
        pct *= _gap_wager_mult(gap, cfg, floor=0.5)
        return _pct_contracts(pct, leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "micro_spread":
        return _pct_contracts(float(cfg["max_bet_pct"]), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "fat_spread":
        return _pct_contracts(float(cfg["max_bet_pct"]), leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "bimodal_spread":
        pct = 0.03 if net_edge < 0.02 else float(cfg["max_bet_pct"])
        return _pct_contracts(pct, leg_cents, bankroll, deployed_cents, cfg)

    if strategy == "gap_inverse":
        max_gap = float(cfg["max_strike_gap"])
        g = gap if gap is not None else 0.0
        tight = 1.0 - min(1.0, g / max_gap) if max_gap > 0 else 1.0
        mult = 0.25 + 0.75 * tight
        return _kelly_contracts(net_edge, leg_cents, bankroll, deployed_cents, cfg, mult)

    # half_kelly, full_kelly, venue_pair, sniper, compound, spread_top3, best_spread_only, gap_tight_only
    gap_mult = _gap_wager_mult(gap, cfg)
    return _kelly_contracts(net_edge, leg_cents, bankroll, deployed_cents, cfg, gap_mult)


def _strategy_allows_entry(strategy: str, r: dict, positions: dict,
                           net_edge: float | None = None,
                           gap: float | None = None) -> bool:
    if strategy == "venue_pair":
        venues = frozenset({r.get("yes_venue"), r.get("no_venue")})
        if venues not in _VENUE_PAIR_ALLOWED:
            return False
    if strategy == "sniper":
        snipe = f"{r['coin']}|{r['expiry']}"
        for p in positions.values():
            if f"{p['coin']}|{p['expiry']}" == snipe:
                return False
    if strategy == "micro_spread":
        if net_edge is None or net_edge < 0.005 or net_edge >= 0.02:
            return False
    if strategy == "fat_spread":
        if net_edge is None or net_edge < 0.08:
            return False
    if strategy == "bimodal_spread":
        if net_edge is None or (net_edge >= 0.02 and net_edge < 0.06):
            return False
    if strategy == "gap_tight_only":
        if gap is None or gap > 0.0003:
            return False
    return True


def _filter_opps_for_strategy(strategy: str, opps: list[dict]) -> list[dict]:
    """Pre-filter/rank opportunities for spread-rank strategies."""
    if not opps:
        return opps
    ranked = sorted(opps, key=lambda r: -(r.get("max_arb") or 0))
    if strategy == "yolo" or strategy == "best_spread_only":
        return ranked[:1]
    if strategy == "spread_top3":
        return ranked[:3]
    return opps


class ArbBot:
    def __init__(self, strategy_id: str = "half_kelly") -> None:
        self._lock = threading.RLock()
        self.cfg = dict(DEFAULTS)
        self.cfg["strategy"] = strategy_id if strategy_id in STRATEGIES else "half_kelly"
        self.running = True          # always on
        self.trade_log: deque = deque(maxlen=LOG_LIMIT)
        self.former_positions: deque = deque(maxlen=100)
        self._started = False
        self.strategy_id = strategy_id

        # Lifetime tracking across busts
        self.life = 1
        self.total_injected_cents = int(round(RELOAD_AMOUNT * 100))
        self.busts: list[dict] = []  # history of bust events
        self.lifetime_realized_cents = 0
        self.equity_curve: list[dict] = []
        self._last_equity_ts = 0.0
        self._funnel = {
            "scans": 0,
            "scanned": 0,
            "eligible": 0,
            "taken": 0,
            "skipped": 0,
            "skip_reasons": {},
            "last_scan": {
                "scanned": 0,
                "eligible": 0,
                "taken": 0,
                "skipped": 0,
                "skip_reasons": {},
                "at": None,
            },
        }

        self.started_at = time.time()

        self._init_ledger()
        self._load_from_db()
        self._apply_strategy(self.cfg["strategy"])
        self._ensure_loop()          # auto-start

    def _init_ledger(self) -> None:
        self.cash_cents = int(round(RELOAD_AMOUNT * 100))
        self.positions: dict[str, dict] = {}
        self.realized_cents = 0
        self.settled_count = 0
        self.wins = 0
        self.equity_curve = []

    def _load_from_db(self) -> None:
        """Load full bot state from database, restoring positions and trade history."""
        try:
            state = bot_persistence.load_bot_state(self.strategy_id)
            if state:
                self.life = int(state.get("life", 1))
                self.cash_cents = int(state.get("cash_cents", int(round(RELOAD_AMOUNT * 100))))
                self.realized_cents = int(state.get("realized_cents", 0))
                self.settled_count = int(state.get("settled_count", 0))
                self.wins = int(state.get("wins", 0))
                self.total_injected_cents = int(state.get("total_injected_cents", int(round(RELOAD_AMOUNT * 100))))
                self.lifetime_realized_cents = int(state.get("lifetime_realized_cents", 0))

            # Restore open positions
            for row in bot_persistence.load_positions(self.strategy_id):
                pos_data = row.get("data") or {}
                key = row["id"]
                self.positions[key] = {
                    "key": key,
                    "coin": row["coin"],
                    "expiry": row["expiry"],
                    "expiry_ts": float(row.get("expiry_ts") or 0),
                    "strike": row.get("strike"),
                    "yes_venue": row.get("yes_venue"),
                    "no_venue": row.get("no_venue"),
                    "yes_cost": float(row.get("yes_cost") or 0),
                    "no_cost": float(row.get("no_cost") or 0),
                    "contracts": int(row.get("contracts") or 0),
                    "cost_cents": int(row.get("cost_cents") or 0),
                    "payout_cents": int(row.get("payout_cents") or 0),
                    "locked_cents": int(row.get("locked_cents") or 0),
                    "gap": float(row.get("gap") or 0),
                    "spread": pos_data.get("spread"),
                    "entry_ts": float(row.get("entry_ts") or 0),
                    "venue_details": pos_data.get("venue_details") or {},
                    "strategy": pos_data.get("strategy") or self.strategy_id,
                }

            # Restore settled trade history into former_positions
            for row in bot_persistence.load_trades(self.strategy_id, limit=500):
                if row.get("status") != "settled":
                    continue
                data = row.get("data") or {}
                self.former_positions.appendleft({
                    "coin": row.get("coin") or data.get("coin"),
                    "expiry": row.get("expiry") or data.get("expiry"),
                    "expiry_ts": data.get("expiry_ts"),
                    "strike": row.get("strike") or data.get("strike"),
                    "yes_venue": data.get("yes_venue"),
                    "no_venue": data.get("no_venue"),
                    "yes_cost": data.get("yes_cost", 0),
                    "no_cost": data.get("no_cost", 0),
                    "yes_strike": data.get("yes_strike"),
                    "no_strike": data.get("no_strike"),
                    "contracts": int(row.get("contracts") or data.get("contracts") or 0),
                    "cost": float(row.get("cost_total") or data.get("cost") or 0),
                    "payout": data.get("payout", 0),
                    "pnl": float(row.get("pnl") or 0),
                    "spot_price": data.get("spot_price"),
                    "winning_leg": data.get("winning_leg"),
                    "outcome": data.get("outcome"),
                    "venue_details": data.get("venue_details") or {},
                    "spread": row.get("spread") or data.get("spread"),
                    "gap": data.get("gap"),
                    "strategy": data.get("strategy") or self.strategy_id,
                    "entry_ts": row.get("entry_ts") or data.get("entry_ts"),
                    "settled_at": row.get("settled_at") or data.get("settled_at"),
                    "price_checked_at": data.get("price_checked_at"),
                    "settlement_confirmed": data.get("settlement_confirmed", True),
                    "life": data.get("life", self.life),
                    "yes_won": data.get("yes_won", False),
                    "no_won": data.get("no_won", False),
                    "yes_pnl": data.get("yes_pnl"),
                    "no_pnl": data.get("no_pnl"),
                })

            self.busts = bot_persistence.load_busts(self.strategy_id)
            self.equity_curve = bot_persistence.load_equity_curve(self.strategy_id)
        except Exception as e:
            print(f"crypto_arb_bot load_from_db error [{self.strategy_id}]: {e}")

    def _bump_skip(self, reasons: dict[str, int], reason: str) -> None:
        reasons[reason] = int(reasons.get(reason) or 0) + 1
        self._funnel["skipped"] = int(self._funnel.get("skipped") or 0) + 1
        lifetime = self._funnel.setdefault("skip_reasons", {})
        lifetime[reason] = int(lifetime.get(reason) or 0) + 1

    def _record_equity(self, equity_cents: int, now: float) -> None:
        if now - self._last_equity_ts < 1.5:
            return
        self._last_equity_ts = now
        pt = {"ts": now, "equity": round(equity_cents / 100.0, 2), "life": self.life}
        if self.equity_curve and self.equity_curve[-1].get("equity") == pt["equity"]:
            return
        self.equity_curve.append(pt)
        if len(self.equity_curve) > 180:
            self.equity_curve = self.equity_curve[-180:]
        try:
            bot_persistence.save_equity_point(self.strategy_id, now, pt["equity"], self.life)
        except Exception as e:
            self._log("error", msg="Failed to save equity point to DB", error=str(e))

    # ---------------------------------------------------------- bust detection
    def _check_bust(self, now: float) -> None:
        """If cash < $1 and no open positions, the life is over."""
        if self.cash_cents >= BUST_THRESHOLD_CENTS or len(self.positions) > 0:
            return
        # Record bust
        bust_equity = self.cash_cents / 100.0
        bust_record = {
            "life": self.life,
            "ts": now,
            "final_cash": bust_equity,
            "realized": self.realized_cents / 100.0,
            "settled": self.settled_count,
            "wins": self.wins,
        }
        self.busts.append(bust_record)
        self._log("bust", life=self.life, final_cash=bust_equity,
                  realized=self.realized_cents / 100.0,
                  settled=self.settled_count, wins=self.wins)

        try:
            bot_persistence.save_bust(
                self.strategy_id, self.life, now, bust_equity,
                self.realized_cents / 100.0, self.settled_count, self.wins
            )
        except Exception as e:
            self._log("error", msg="Failed to save bust to DB", error=str(e))

        # Carry over lifetime P&L
        self.lifetime_realized_cents += self.realized_cents

        # Reload
        self.life += 1
        self.total_injected_cents += int(round(RELOAD_AMOUNT * 100))
        self._init_ledger()
        self._log("reload", life=self.life,
                  amount=RELOAD_AMOUNT,
                  total_injected=self.total_injected_cents / 100.0)

        self._save_to_db()

    def _save_to_db(self) -> None:
        """Persist current bot state to database."""
        try:
            bot_persistence.save_bot_state(
                self.strategy_id,
                self.life,
                self.cash_cents,
                self.realized_cents,
                self.settled_count,
                self.wins,
                self.total_injected_cents,
                self.lifetime_realized_cents,
            )
        except Exception as e:
            self._log("error", msg="Failed to save bot state to DB", error=str(e))

    # ---------------------------------------------------------- config
    def _apply_strategy(self, strategy_id: str) -> None:
        spec = STRATEGIES.get(strategy_id)
        if not spec:
            return
        self.cfg["strategy"] = strategy_id
        for k, v in spec.items():
            if k in ("label", "desc"):
                continue
            if k in self.cfg and isinstance(v, (int, float)):
                self.cfg[k] = v

    def set_config(self, updates: dict) -> dict:
        with self._lock:
            for k, v in (updates or {}).items():
                if k == "strategy" and isinstance(v, str) and v in STRATEGIES:
                    self._apply_strategy(v)
                elif k in self.cfg and isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.cfg[k] = v
        return self.snapshot()

    # ---------------------------------------------------------- loop
    def _ensure_loop(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="crypto-arb-bot").start()

    def _loop(self) -> None:
        last_save = time.time()
        while True:
            try:
                snap = crypto_arb.snapshot()
                now = time.time()
                with self._lock:
                    self._settle(now)
                    self._check_bust(now)
                    self._enter(snap.get("opportunities") or [], now)
                    if now - last_save > 30:
                        self._save_to_db()
                        last_save = now
            except Exception as e:
                print("crypto_arb_bot loop error:", e)
            time.sleep(float(self.cfg["poll_interval"]))

    # ---------------------------------------------------------- entries
    def _enter(self, opps: list[dict], now: float) -> None:
        c = self.cfg
        strategy = str(c.get("strategy") or "half_kelly")
        fee_leg = float(c["fee_cents"])
        raw_scanned = len(opps or [])

        # Rank / filter by spread width before entry loop
        opps = _filter_opps_for_strategy(strategy, opps)
        scan_reasons: dict[str, int] = {}
        scanned = len(opps)
        eligible = 0
        taken = 0
        skipped = 0
        self._funnel["scans"] = int(self._funnel.get("scans") or 0) + 1
        self._funnel["scanned"] = int(self._funnel.get("scanned") or 0) + scanned
        if raw_scanned > scanned:
            trimmed = raw_scanned - scanned
            skipped += trimmed
            self._funnel["skipped"] = int(self._funnel.get("skipped") or 0) + trimmed
            lifetime = self._funnel.setdefault("skip_reasons", {})
            lifetime["rank_filtered"] = int(lifetime.get("rank_filtered") or 0) + trimmed
            scan_reasons["rank_filtered"] = trimmed

        for idx, r in enumerate(opps):
            if len(self.positions) >= int(c["max_positions"]):
                rest = len(opps) - idx
                for _ in range(rest):
                    self._bump_skip(scan_reasons, "max_positions")
                skipped += rest
                break
            deployed_cents = sum(p["cost_cents"] for p in self.positions.values())
            open_payout = sum(p["payout_cents"] for p in self.positions.values())
            equity_cents = self.cash_cents + open_payout
            yes_cost, no_cost = r.get("yes_cost"), r.get("no_cost")
            if yes_cost is None or no_cost is None:
                skipped += 1
                self._bump_skip(scan_reasons, "missing_costs")
                continue
            per_venue_raw = r.get("per_venue") or {}
            yes_venue, no_venue = r.get("yes_venue"), r.get("no_venue")
            yes_detail = per_venue_raw.get(yes_venue) or {}
            no_detail = per_venue_raw.get(no_venue) or {}
            if (
                yes_venue == no_venue
                or r.get("strike_verified") is not True
                or r.get("settlement_rules_verified") is not True
                or yes_detail.get("strike_verified") is not True
                or no_detail.get("strike_verified") is not True
                or yes_detail.get("settlement_rule_verified") is not True
                or no_detail.get("settlement_rule_verified") is not True
                or not crypto_arb._payout_coverage(yes_detail, no_detail)
            ):
                skipped += 1
                self._bump_skip(scan_reasons, "not_verified")
                continue
            key = (
                f"{r['coin']}|{r['expiry']}|{yes_venue}|{yes_detail.get('strike')}"
                f"|{no_venue}|{no_detail.get('strike')}"
            )
            if key in self.positions:
                skipped += 1
                self._bump_skip(scan_reasons, "already_open")
                continue
            exp_ts = _expiry_ts(r.get("expiry"))
            if exp_ts is None or exp_ts <= now:
                skipped += 1
                self._bump_skip(scan_reasons, "expired")
                continue

            gap = r.get("strike_gap")
            if gap is None:
                gap = _strike_gap_frac(r.get("per_venue") or {})
            if gap is None or gap > float(c["max_strike_gap"]):
                skipped += 1
                self._bump_skip(scan_reasons, "strike_gap")
                continue

            leg_cents = round(yes_cost * 100) + round(no_cost * 100) + 2 * fee_leg
            net_edge = (100 - leg_cents) / 100.0
            if net_edge < float(c["min_edge"]):
                skipped += 1
                self._bump_skip(scan_reasons, "below_min_edge")
                continue

            if not _strategy_allows_entry(strategy, r, self.positions,
                                          net_edge=net_edge, gap=gap):
                skipped += 1
                self._bump_skip(scan_reasons, "strategy_filter")
                continue

            eligible += 1

            n = _strategy_contracts(
                strategy, net_edge, int(round(leg_cents)),
                self.cash_cents, deployed_cents, equity_cents, c, exp_ts, now, gap)
            if n < 1:
                skipped += 1
                self._bump_skip(scan_reasons, "size_zero")
                continue

            cost_cents = int(round(leg_cents * n))
            if cost_cents > self.cash_cents:
                n = int(self.cash_cents / leg_cents)
                if n < 1:
                    skipped += 1
                    self._bump_skip(scan_reasons, "insufficient_cash")
                    continue
                cost_cents = int(round(leg_cents * n))

            payout = 100 * n
            locked = payout - cost_cents

            venue_details = {}
            for v, vdata in per_venue_raw.items():
                venue_details[v] = {
                    "strike": crypto_arb.normalize_strike(vdata.get("strike")),
                    "yes": vdata.get("yes"),
                    "no": vdata.get("no"),
                    "strike_verified": vdata.get("strike_verified") is True,
                    "strike_source": vdata.get("strike_source"),
                    "strike_evidence": vdata.get("strike_evidence"),
                    "strike_timestamp_ms": vdata.get("strike_timestamp_ms"),
                    "strike_delay_ms": vdata.get("strike_delay_ms"),
                    "settlement_rule_verified": vdata.get("settlement_rule_verified") is True,
                    "yes_operator": vdata.get("yes_operator"),
                    "no_operator": vdata.get("no_operator"),
                    "rule_evidence": vdata.get("rule_evidence"),
                }
                if vdata.get("slug"):
                    venue_details[v]["slug"] = vdata["slug"]
                if vdata.get("ticker"):
                    venue_details[v]["ticker"] = vdata["ticker"]

            self.cash_cents -= cost_cents
            deployed_cents += cost_cents
            pos_data = {
                "key": key,
                "coin": r["coin"],
                "expiry": r["expiry"],
                "expiry_ts": exp_ts,
                "strike": r["strike"],
                "yes_venue": r["yes_venue"],
                "no_venue": r["no_venue"],
                "yes_cost": yes_cost,
                "no_cost": no_cost,
                "contracts": n,
                "cost_cents": cost_cents,
                "payout_cents": payout,
                "locked_cents": locked,
                "gap": gap,
                "spread": round(net_edge, 4),
                "entry_ts": now,
                "venue_details": venue_details,
                "strategy": strategy,
            }
            self.positions[key] = pos_data

            try:
                bot_persistence.save_position(
                    self.strategy_id, key, r["coin"], r["expiry"],
                    r["strike"], r["yes_venue"], r["no_venue"],
                    yes_cost, no_cost, n, cost_cents, locked, payout,
                    gap, now, exp_ts, pos_data
                )
            except Exception as e:
                self._log("error", msg="Failed to save position to DB", error=str(e))

            taken += 1
            self._funnel["taken"] = int(self._funnel.get("taken") or 0) + 1
            self._log("enter", coin=r["coin"], strike=r["strike"], expiry=r["expiry"],
                      contracts=n, yes_venue=r["yes_venue"], no_venue=r["no_venue"],
                      yes_cost=yes_cost, no_cost=no_cost,
                      cost_cents=cost_cents, locked_cents=locked,
                      edge=round(net_edge, 4), spread=round(net_edge, 4),
                      strike_gap=round(gap, 6) if gap is not None else None,
                      strategy=strategy,
                      venue_details=venue_details)

            if strategy in ("yolo", "best_spread_only"):
                break

        self._funnel["eligible"] = int(self._funnel.get("eligible") or 0) + eligible
        self._funnel["last_scan"] = {
            "scanned": scanned,
            "eligible": eligible,
            "taken": taken,
            "skipped": skipped,
            "skip_reasons": dict(sorted(scan_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "at": now,
        }

    # ---------------------------------------------------------- settlement
    def _settle(self, now: float) -> None:
        to_settle = [k for k in self.positions if now >= self.positions[k]["expiry_ts"]]
        if not to_settle:
            return

        spot: dict[str, float] = {}
        try:
            spot = crypto_arb._fetch_spot()
        except Exception:
            pass

        for key in to_settle:
            pos = self.positions[key]
            coin = pos["coin"]
            spot_price = spot.get(coin)
            if spot_price is None:
                # Do not guess an outcome. Keep the expired trade pending and
                # retry until a real post-expiry price can be obtained.
                continue

            venue_details = pos.get("venue_details") or {}
            yes_venue = pos["yes_venue"]
            no_venue = pos["no_venue"]
            yes_detail = venue_details.get(yes_venue) or {}
            no_detail = venue_details.get(no_venue) or {}
            yes_strike = crypto_arb.normalize_strike(yes_detail.get("strike") or pos["strike"])
            no_strike = crypto_arb.normalize_strike(no_detail.get("strike") or pos["strike"])
            yes_operator = yes_detail.get("yes_operator")
            no_operator = no_detail.get("no_operator")
            if (
                yes_detail.get("settlement_rule_verified") is not True
                or no_detail.get("settlement_rule_verified") is not True
                or not crypto_arb._payout_coverage(yes_detail, no_detail)
            ):
                continue

            outcome = None
            winning_leg = None
            yes_won = False
            no_won = False
            if spot_price is not None:
                yes_won = _threshold_won(spot_price, yes_strike, yes_operator)
                no_won = _threshold_won(spot_price, no_strike, no_operator)
                spot_txt = crypto_arb.format_price(spot_price)
                yes_txt = crypto_arb.format_strike(yes_strike)
                no_txt = crypto_arb.format_strike(no_strike)
                if yes_won and no_won:
                    winning_leg = "BOTH"
                    outcome = (
                        f"Spot ${spot_txt} {yes_operator} YES strike ${yes_txt} AND "
                        f"{no_operator} NO strike ${no_txt} → BOTH pay!"
                    )
                elif yes_won:
                    winning_leg = "YES"
                    outcome = f"Spot ${spot_txt} {yes_operator} YES strike ${yes_txt} → YES pays"
                elif no_won:
                    winning_leg = "NO"
                    outcome = f"Spot ${spot_txt} {no_operator} NO strike ${no_txt} → NO pays"
                else:
                    winning_leg = "NEITHER"
                    outcome = (
                        f"Spot ${spot_txt} is between NO strike ${no_txt} and "
                        f"YES strike ${yes_txt} → BOTH lose!"
                    )
            yes_payout = 100 if yes_won else 0
            no_payout = 100 if no_won else 0
            
            contracts = pos["contracts"]
            yes_cost_cents = round(pos["yes_cost"] * 100)
            no_cost_cents = round(pos["no_cost"] * 100)
            
            yes_pnl_cents = (yes_payout - yes_cost_cents) * contracts
            no_pnl_cents = (no_payout - no_cost_cents) * contracts
            
            actual_payout_cents = (yes_payout + no_payout) * contracts
            actual_pnl_cents = actual_payout_cents - pos["cost_cents"]

            self.cash_cents += actual_payout_cents
            self.realized_cents += actual_pnl_cents
            self.settled_count += 1
            if actual_pnl_cents > 0:
                self.wins += 1

            try:
                bot_persistence.save_trade(
                    pos.get("key") or key,
                    self.strategy_id,
                    "settled",
                    coin,
                    pos["expiry"],
                    pos["strike"],
                    contracts,
                    pos["cost_cents"] / 100.0,
                    pos.get("locked_cents", 0) / 100.0,
                    actual_pnl_cents / 100.0,
                    pos.get("spread"),
                    pos.get("entry_ts"),
                    now,
                    pos
                )
                bot_persistence.remove_position(pos.get("key") or key)
            except Exception as e:
                self._log("error", msg="Failed to save settled trade to DB", error=str(e))

            del self.positions[key]

            spot_store = float(crypto_arb.price_decimal(spot_price) or spot_price)

            former_pos = {
                "coin": coin,
                "expiry": pos["expiry"],
                "expiry_ts": pos["expiry_ts"],
                "strike": pos["strike"],
                "yes_venue": yes_venue,
                "no_venue": no_venue,
                "yes_cost": pos["yes_cost"],
                "no_cost": pos["no_cost"],
                "yes_strike": yes_strike,
                "no_strike": no_strike,
                "contracts": contracts,
                "cost": pos["cost_cents"] / 100.0,
                "payout": actual_payout_cents / 100.0,
                "pnl": actual_pnl_cents / 100.0,
                "spot_price": spot_store,
                "winning_leg": winning_leg,
                "outcome": outcome,
                "venue_details": venue_details,
                "spread": pos.get("spread"),
                "gap": pos.get("gap"),
                "strategy": pos.get("strategy"),
                "entry_ts": pos.get("entry_ts"),
                "settled_at": now,
                "price_checked_at": now,
                "settlement_confirmed": True,
                "life": self.life,
                "yes_won": yes_won,
                "no_won": no_won,
                "yes_pnl": yes_pnl_cents / 100.0,
                "no_pnl": no_pnl_cents / 100.0
            }
            self.former_positions.appendleft(former_pos)

            self._log("settle", coin=coin, strike=pos["strike"],
                      expiry=pos["expiry"], contracts=pos["contracts"],
                      pnl_cents=actual_pnl_cents,
                      yes_pnl_cents=yes_pnl_cents, no_pnl_cents=no_pnl_cents,
                      yes_won=yes_won, no_won=no_won,
                      cost_cents=pos["cost_cents"],
                      spot_price=spot_store,
                      yes_venue=yes_venue, no_venue=no_venue,
                      yes_strike=yes_strike, no_strike=no_strike,
                      yes_cost=pos["yes_cost"], no_cost=pos["no_cost"],
                      spread=pos.get("spread"), strike_gap=pos.get("gap"),
                      strategy=pos.get("strategy"),
                      winning_leg=winning_leg, outcome=outcome,
                      venue_details=venue_details)

    def _log(self, kind: str, **fields) -> None:
        entry = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "kind": kind,
                 "life": self.life}
        entry.update(fields)
        self.trade_log.appendleft(entry)

    def _backfill_former_positions(self) -> None:
        """Rebuild settled-position cards from the trade log after deploys."""
        if len(self.former_positions) >= self.settled_count:
            return
        seen: set[tuple] = set()
        for p in self.former_positions:
            seen.add((p.get("coin"), p.get("expiry"), p.get("strike"), p.get("settled_at")))
        for e in reversed(self.trade_log):
            if e.get("kind") != "settle":
                continue
            dedup = (e.get("coin"), e.get("expiry"), e.get("strike"), e.get("ts"))
            if dedup in seen:
                continue
            seen.add(dedup)
            contracts = int(e.get("contracts") or 0)
            yes_cost = float(e.get("yes_cost") or 0)
            no_cost = float(e.get("no_cost") or 0)
            yes_pnl = e.get("yes_pnl_cents")
            no_pnl = e.get("no_pnl_cents")
            if yes_pnl is None or no_pnl is None:
                win = (e.get("winning_leg") or "").upper()
                yes_won = e.get("yes_won")
                no_won = e.get("no_won")
                if yes_won is None:
                    yes_won = win in ("YES", "BOTH")
                if no_won is None:
                    no_won = win in ("NO", "BOTH")
                yes_pnl, no_pnl = _leg_pnls_from_settle(
                    contracts, yes_cost, no_cost, bool(yes_won), bool(no_won))
                yes_pnl = int(round(yes_pnl * 100))
                no_pnl = int(round(no_pnl * 100))
            self.former_positions.appendleft({
                "coin": e.get("coin"),
                "expiry": e.get("expiry"),
                "expiry_ts": _expiry_ts(e.get("expiry") or ""),
                "strike": e.get("strike"),
                "yes_venue": e.get("yes_venue"),
                "no_venue": e.get("no_venue"),
                "yes_cost": yes_cost,
                "no_cost": no_cost,
                "yes_strike": e.get("yes_strike"),
                "no_strike": e.get("no_strike"),
                "contracts": contracts,
                "cost": round(contracts * (yes_cost + no_cost), 2),
                "pnl": round((e.get("pnl_cents") or 0) / 100.0, 2),
                "spot_price": e.get("spot_price"),
                "winning_leg": e.get("winning_leg"),
                "outcome": e.get("outcome"),
                "venue_details": e.get("venue_details") or {},
                "spread": e.get("spread") if e.get("spread") is not None
                          else round(1 - yes_cost - no_cost, 4),
                "gap": e.get("strike_gap"),
                "strategy": e.get("strategy"),
                "entry_ts": e.get("entry_ts"),
                "settled_at": e.get("ts"),
                "life": e.get("life"),
                "yes_won": e.get("yes_won"),
                "no_won": e.get("no_won"),
                "yes_pnl": round((yes_pnl or 0) / 100.0, 2),
                "no_pnl": round((no_pnl or 0) / 100.0, 2),
            })

    # ---------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._lock:
            self._backfill_former_positions()
            now = time.time()
            positions = []
            open_payout = 0
            locked_pending = 0
            deployed_cents = 0
            for pos in self.positions.values():
                open_payout += pos["payout_cents"]
                locked_pending += pos["locked_cents"]
                deployed_cents += pos["cost_cents"]
                vd = pos.get("venue_details") or {}
                venue_info = {}
                for v, detail in vd.items():
                    venue_info[v] = {
                        "strike": detail.get("strike"),
                        "yes": detail.get("yes"),
                        "no": detail.get("no"),
                        "strike_verified": detail.get("strike_verified") is True,
                        "strike_source": detail.get("strike_source"),
                        "strike_evidence": detail.get("strike_evidence"),
                        "strike_timestamp_ms": detail.get("strike_timestamp_ms"),
                        "strike_delay_ms": detail.get("strike_delay_ms"),
                        "settlement_rule_verified": detail.get("settlement_rule_verified") is True,
                        "yes_operator": detail.get("yes_operator"),
                        "no_operator": detail.get("no_operator"),
                        "rule_evidence": detail.get("rule_evidence"),
                    }
                    if detail.get("slug"):
                        venue_info[v]["slug"] = detail["slug"]
                    if detail.get("ticker"):
                        venue_info[v]["ticker"] = detail["ticker"]
                positions.append({
                    "coin": pos["coin"],
                    "expiry": pos["expiry"],
                    "strike": pos["strike"],
                    "yes_venue": pos["yes_venue"],
                    "no_venue": pos["no_venue"],
                    "yes_cost": pos["yes_cost"],
                    "no_cost": pos["no_cost"],
                    "contracts": pos["contracts"],
                    "cost": pos["cost_cents"] / 100.0,
                    "locked": pos["locked_cents"] / 100.0,
                    "gap_pct": round(pos["gap"] * 100, 3),
                    "age_s": int(now - pos["entry_ts"]),
                    "settles_in_s": max(0, int(pos["expiry_ts"] - now)),
                    "venue_details": venue_info,
                })

            equity_cents = self.cash_cents + open_payout
            total_lifetime_realized = self.lifetime_realized_cents + self.realized_cents
            total_injected = self.total_injected_cents
            # Lifetime P&L = current equity + realized from past lives - total injected
            lifetime_pnl_cents = equity_cents + (self.lifetime_realized_cents) - total_injected
            # But realized from current life is already in equity via cash, so:
            # lifetime_pnl = equity - (injected for this life) + past_lives_realized - past_lives_injected
            # Simpler: lifetime_pnl = equity + sum(past bust final_cash) - total_injected
            # Actually simplest: lifetime_pnl = current equity + total past realized - total injected
            # Since past lives ended with busts, their "realized" was already cashed.
            # Let me think... at bust, remaining cash was discarded and we track it.
            # Lifetime net = current_equity - total_injected + sum(bust_final_cash for each bust that had leftover)
            # No — at bust, cash < $1 is just lost. So:
            lifetime_pnl_cents = equity_cents - total_injected
            for b in self.busts:
                lifetime_pnl_cents += int(round(b["final_cash"] * 100))

            ret_pct = (lifetime_pnl_cents / total_injected * 100) if total_injected else 0.0
            confirmed_ret_pct = (
                total_lifetime_realized / total_injected * 100
            ) if total_injected else 0.0

            self._record_equity(equity_cents, now)

            positions.sort(key=lambda p: p["settles_in_s"])
            open_trades = [_trade_from_open(pos, now) for pos in self.positions.values()]
            open_trades.sort(key=lambda t: t.get("settles_in_s") or 0)
            settled_trades = [_trade_from_settled(p) for p in self.former_positions]
            trades = open_trades + settled_trades
            leg_pnl_total = round(sum(
                leg.get("pnl", 0) for t in settled_trades for leg in t.get("legs", [])
                if leg.get("pnl") is not None
            ), 2)
            risk_metrics = _risk_metrics(
                settled_trades,
                open_trades,
                self.equity_curve,
                equity_cents / 100.0,
                deployed_cents / 100.0,
            )
            funnel = getattr(self, "_funnel", None) or {
                "scans": 0, "scanned": 0, "eligible": 0, "taken": 0, "skipped": 0,
                "skip_reasons": {}, "last_scan": {},
            }
            return {
                "running": True,  # always on
                "life": self.life,
                "polling_age_s": int(now - self.started_at),
                "strategy": self.cfg.get("strategy", "half_kelly"),
                "strategies": {
                    k: {
                        "label": v["label"],
                        "desc": v["desc"],
                        **STRATEGY_TECHNICAL.get(k, {}),
                    }
                    for k, v in STRATEGIES.items()
                },
                "total_injected": round(total_injected / 100.0, 2),
                "busts": len(self.busts),
                "bust_history": self.busts[-10:],  # last 10
                "config": dict(self.cfg),
                "starting_balance": RELOAD_AMOUNT,
                "cash": round(self.cash_cents / 100.0, 2),
                "equity": round(equity_cents / 100.0, 2),
                "deployed": round(deployed_cents / 100.0, 2),
                "realized": round(self.realized_cents / 100.0, 2),
                "lifetime_realized": round(total_lifetime_realized / 100.0, 2),
                "confirmed_pnl": round(total_lifetime_realized / 100.0, 2),
                "lifetime_pnl": round(lifetime_pnl_cents / 100.0, 2),
                "locked_pending": round(locked_pending / 100.0, 2),
                "return_pct": round(ret_pct, 3),
                "confirmed_return_pct": round(confirmed_ret_pct, 3),
                "open_count": len(self.positions),
                "settled_count": self.settled_count,
                "wins": self.wins,
                "positions": positions,
                "former_positions": list(self.former_positions),
                "trades": trades,
                "leg_pnl_total": leg_pnl_total,
                "risk_metrics": risk_metrics,
                "equity_curve": list(self.equity_curve),
                "funnel": {
                    "scans": int(funnel.get("scans") or 0),
                    "scanned": int(funnel.get("scanned") or 0),
                    "eligible": int(funnel.get("eligible") or 0),
                    "taken": int(funnel.get("taken") or 0),
                    "skipped": int(funnel.get("skipped") or 0),
                    "skip_reasons": dict(
                        sorted(
                            (funnel.get("skip_reasons") or {}).items(),
                            key=lambda kv: (-int(kv[1]), kv[0]),
                        )
                    ),
                    "last_scan": dict(funnel.get("last_scan") or {}),
                    "take_rate_pct": (
                        round(
                            int(funnel.get("taken") or 0)
                            / int(funnel.get("scanned") or 0)
                            * 100,
                            2,
                        )
                        if int(funnel.get("scanned") or 0)
                        else None
                    ),
                },
                "log": list(self.trade_log)[:80],
                "updated": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            }


# ------------------------------------------------------------------ module API
_STRATEGY_BOTS: dict[str, ArbBot] = {}
_STRATEGY_BOTS_LOCK = threading.RLock()


def snapshot() -> dict:
    bot = get_strategy_bot("half_kelly")
    return bot.snapshot()  # type: ignore[union-attr]


def set_config(updates: dict) -> dict:
    bot = get_strategy_bot("half_kelly")
    return bot.set_config(updates)  # type: ignore[union-attr]


def get_strategy_bot(strategy_id: str) -> ArbBot | None:
    """Return the independent paper ledger for one sizing strategy."""
    if strategy_id not in STRATEGIES:
        return None
    with _STRATEGY_BOTS_LOCK:
        bot = _STRATEGY_BOTS.get(strategy_id)
        if bot is None:
            bot = ArbBot(strategy_id)
            _STRATEGY_BOTS[strategy_id] = bot
        return bot


def strategy_snapshot(strategy_id: str) -> dict | None:
    bot = get_strategy_bot(strategy_id)
    return bot.snapshot() if bot else None


def all_strategy_snapshots() -> dict[str, dict]:
    """Start and snapshot every strategy bot for side-by-side comparison."""
    return {
        strategy_id: get_strategy_bot(strategy_id).snapshot()  # type: ignore[union-attr]
        for strategy_id in STRATEGIES
    }
