"""Execute crypto_arb opportunities with per-user venue credentials."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from urllib.request import Request, urlopen

import autopilot_store
from arb import cryptocom_trade, user_venue

log = logging.getLogger(__name__)


def _gamma_market_by_slug(slug: str) -> dict:
    if not slug:
        return {}
    try:
        req = Request(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            headers={"Accept": "application/json", "User-Agent": "autopilot/1.0"},
        )
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        evs = data if isinstance(data, list) else []
        if not evs or not evs[0].get("markets"):
            return {}
        m = evs[0]["markets"][0]
        toks = json.loads(m.get("clobTokenIds") or "[]")
        return {
            "market_id": str(m.get("id") or ""),
            "token_yes": toks[0] if toks else "",
            "token_no": toks[1] if len(toks) > 1 else "",
        }
    except Exception as e:
        log.warning("gamma slug lookup %s: %s", slug, e)
        return {}


def venue_balances(user_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"venues": {}, "ready": []}
    for venue in ("kalshi", "polymarket", "cryptocom"):
        creds = autopilot_store.get_venue_credentials(user_id, venue)
        if not creds:
            out["venues"][venue] = {"connected": False}
            continue
        if venue == "kalshi":
            bal = user_venue.kalshi_balance(creds)
        elif venue == "polymarket":
            bal = user_venue.polymarket_balance(creds)
        else:
            bal = cryptocom_trade.balance(creds)
        out["venues"][venue] = bal
        if not bal.get("error"):
            out["ready"].append(venue)
    return out


def required_venues(opp: dict) -> set[str]:
    out: set[str] = set()
    for key in ("yes_venue", "no_venue"):
        venue = opp.get(key)
        if venue:
            out.add(str(venue))
    return out


def connected_venues(user_id: str) -> set[str]:
    return {
        venue
        for venue in autopilot_store.VENUES
        if autopilot_store.get_venue_credentials(user_id, venue)
    }


def filter_opportunities(user_id: str, opps: list[dict]) -> list[dict]:
    """Keep only arbs whose YES/NO venues are all linked for this user."""
    return [opp for opp in opps if can_execute(user_id, opp)[0]]


def can_execute(user_id: str, opp: dict) -> tuple[bool, str]:
    needed = required_venues(opp)
    if not needed:
        return False, "no_venues"
    for venue in needed:
        if venue not in autopilot_store.VENUES:
            return False, f"unsupported_venue:{venue}"
        if not autopilot_store.get_venue_credentials(user_id, venue):
            return False, f"{venue}_not_connected"
    return True, "ok"


def _cryptocom_leg(user_id: str, side: str, opp: dict, per_venue: dict, live_mode: bool) -> dict[str, Any]:
    creds = autopilot_store.get_venue_credentials(user_id, "cryptocom") or {}
    detail = per_venue.get("cryptocom") or {}
    contract_id = detail.get("contract_id") or detail.get("symbol")
    price = float(opp.get("yes_cost") if side == "yes" else opp.get("no_cost") or 0)
    if not contract_id:
        return {"error": "Crypto.com contract_id missing from quote"}
    if not live_mode:
        return {"paper": True, "venue": "cryptocom", "side": side, "contract_id": contract_id, "price": price}
    return cryptocom_trade.place_predict_order(
        creds,
        contract_id=str(contract_id),
        side="BUY",
        price=price,
        quantity=1.0,
    )


def execute_opportunity(
    user_id: str,
    opp: dict,
    *,
    contracts: int,
    live_mode: bool,
) -> dict[str, Any]:
    """Place YES/NO legs for a crypto_arb opportunity."""
    arb_id = uuid.uuid4().hex[:12]
    ok, reason = can_execute(user_id, opp)
    if not ok:
        return {"arb_id": arb_id, "ok": False, "error": reason}

    yes_venue = opp.get("yes_venue")
    no_venue = opp.get("no_venue")
    yes_cost = float(opp.get("yes_cost") or 0)
    no_cost = float(opp.get("no_cost") or 0)
    per_venue = opp.get("per_venue") or {}
    legs: dict[str, Any] = {}
    errors: list[str] = []

    if yes_venue == "kalshi":
        ticker = (per_venue.get("kalshi") or {}).get("ticker")
        if not ticker:
            errors.append("Kalshi YES: missing ticker")
        else:
            creds = autopilot_store.get_venue_credentials(user_id, "kalshi")
            legs["kalshi_yes"] = user_venue.kalshi_place_order(
                creds, ticker, "yes", contracts, int(round(yes_cost * 100))
            )
            if legs["kalshi_yes"].get("error"):
                errors.append(f"Kalshi YES: {legs['kalshi_yes']['error']}")

    if no_venue == "kalshi":
        ticker = (per_venue.get("kalshi") or {}).get("ticker")
        if not ticker:
            errors.append("Kalshi NO: missing ticker")
        else:
            creds = autopilot_store.get_venue_credentials(user_id, "kalshi")
            legs["kalshi_no"] = user_venue.kalshi_place_order(
                creds, ticker, "no", contracts, int(round(no_cost * 100))
            )
            if legs["kalshi_no"].get("error"):
                errors.append(f"Kalshi NO: {legs['kalshi_no']['error']}")

    poly_creds = autopilot_store.get_venue_credentials(user_id, "polymarket")

    for side, venue_key in (("yes", yes_venue), ("no", no_venue)):
        if venue_key == "polymarket":
            detail = per_venue.get("polymarket") or {}
            slug = detail.get("slug")
            meta = _gamma_market_by_slug(slug)
            token = meta.get("token_yes") if side == "yes" else meta.get("token_no")
            price = yes_cost if side == "yes" else no_cost
            if not token:
                errors.append(f"Poly {side.upper()}: missing token (slug={slug})")
                continue
            legs[f"poly_{side}"] = user_venue.polymarket_buy(
                poly_creds or {},
                market_id=meta.get("market_id", ""),
                token_id=token,
                price=price,
                size=float(contracts),
                live=live_mode,
            )
            if legs[f"poly_{side}"].get("error"):
                errors.append(f"Poly {side.upper()}: {legs[f'poly_{side}']['error']}")
        elif venue_key == "cryptocom":
            leg = _cryptocom_leg(user_id, side, opp, per_venue, live_mode)
            legs[f"cc_{side}"] = leg
            if leg.get("error"):
                errors.append(f"Crypto.com {side.upper()}: {leg['error']}")

    result = {
        "arb_id": arb_id,
        "ts": time.time(),
        "ok": not errors,
        "errors": errors,
        "legs": legs,
        "contracts": contracts,
        "live_mode": live_mode,
        "coin": opp.get("coin"),
        "expiry": opp.get("expiry"),
        "edge": opp.get("max_arb"),
    }
    autopilot_store.append_log(
        user_id,
        "info" if result["ok"] else "error",
        f"{'Live' if live_mode else 'Paper'} arb {opp.get('coin')} x{contracts}",
        result,
    )
    try:
        autopilot_store.save_trade(user_id, result)
    except Exception:
        log.exception("save_trade failed user=%s", user_id)
    return result
