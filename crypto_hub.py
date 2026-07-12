"""Crypto markets hub — overview, series, market detail, live refresh."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import catalog
import catalog_db
import market_activity
import rate_limit
from http_util import http_get

EXPLORE_CATEGORIES = [
    "Politics", "Crypto", "Sports", "Economics", "Entertainment",
    "Esports", "Weather", "Culture", "Tech", "World", "Financials",
]
_CATEGORY_ALIASES = {
    "elections": "Politics", "politics": "Politics", "mentions": "Politics",
    "trump": "Politics", "midterms": "Politics", "nov 4 elections": "Politics",
    "geopolitics": "World", "world": "World",
    "crypto": "Crypto", "crypto prices": "Crypto", "up or down": "Crypto",
    "sports": "Sports", "tennis": "Sports", "soccer": "Sports",
    "economics": "Economics", "commodities": "Economics", "companies": "Financials",
    "financials": "Financials",
    "entertainment": "Entertainment", "culture": "Culture", "social": "Culture",
    "esports": "Esports", "games": "Esports",
    "climate and weather": "Weather",
    "science and technology": "Tech", "tech": "Tech",
}


def _explore_category(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return "Other"
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    for token, label in _CATEGORY_ALIASES.items():
        if token in key:
            return label
    return raw.strip().title()

_ACTIVITY_CACHE: dict = {}
_ACTIVITY_TTL = 25

_CC_LIVE_TTL = 3.0
_cc_live_lock = threading.Lock()
_CC_LIVE_CACHE: dict[str, tuple[float, dict]] = {}


def _cryptocom_live_contracts(event_id: str) -> dict[str, dict]:
    """Fresh contract quotes for one crypto.com event, keyed by contract id.
    Short shared TTL so any number of page viewers cost one upstream curl
    every few seconds (the fetch shells out to curl — see catalog._cryptocom_get)."""
    now = time.time()
    with _cc_live_lock:
        hit = _CC_LIVE_CACHE.get(event_id)
        if hit and now - hit[0] < _CC_LIVE_TTL:
            return hit[1]
    contracts: dict[str, dict] = {}
    try:
        for c in catalog._fetch_cryptocom_contracts([event_id]).get(event_id) or []:
            if c.get("id") is not None:
                contracts[str(c["id"])] = c
    except Exception:
        contracts = {}
    with _cc_live_lock:
        _CC_LIVE_CACHE[event_id] = (now, contracts)
        if len(_CC_LIVE_CACHE) > 200:
            cutoff = now - _CC_LIVE_TTL
            for k in [k for k, (t, _) in _CC_LIVE_CACHE.items() if t < cutoff]:
                _CC_LIVE_CACHE.pop(k, None)
    return contracts


def market_payload(venue: str, key: str) -> dict | None:
    """Full single-event detail for the market page, sourced from the catalog."""
    ev = catalog_db.get_one(venue, key)
    if not ev and not catalog.cache_stats().get("ready"):
        return None
    if not ev:
        return None
    markets = []
    for m in ev.get("markets") or []:
        last = m.get("last")
        bid, ask = m.get("bid"), m.get("ask")
        # Fall back to the bid/ask midpoint only for a real spread — a 0/100 book
        # is a placeholder (no quotes), not a genuine 50% probability.
        if last is None and bid is not None and ask is not None and not (bid == 0 and ask == 100):
            last = round((bid + ask) / 2)
        markets.append({
            "label": m.get("label") or "",
            "slug": market_activity.outcome_slug(m.get("label") or ""),
            "yes": last,
            "bid": bid,
            "ask": ask,
            "ticker": m.get("ticker"),
            "id": m.get("id"),
            "image": m.get("image") or m.get("icon") or "",
            "end_date": m.get("end_date"),
        })
    extra_info = {}
    try:
        # The catalog snapshot behind this payload can be minutes old; the page polls
        # every ~4s, so pull the live event too and let its prices/dates win. A short
        # TTL keeps this at roughly one upstream request per market per few seconds
        # no matter how many viewers poll.
        if venue == "polymarket":
            raw = http_get(f"https://gamma-api.polymarket.com/events?slug={key}", ttl=2)
            if isinstance(raw, list) and raw:
                r = raw[0]
            elif isinstance(raw, dict) and raw.get("data"):
                r = raw["data"][0]
            else:
                r = {}
            if r:
                desc = r.get("description", "")
                if not desc:
                    desc = (r.get("eventMetadata") or {}).get("context_description", "")
                extra_info["description"] = desc
                extra_info["resolution_source"] = r.get("resolutionSource", "")
                extra_info["end_date"] = r.get("endDate") or r.get("endDateIso")

                live_by_id = {str(lm.get("id")): lm for lm in r.get("markets") or []}
                for m in markets:
                    lm = live_by_id.get(str(m.get("id") or ""))
                    if not lm:
                        continue
                    m["end_date"] = lm.get("endDate") or lm.get("endDateIso") or m["end_date"]
                    bid, ask = lm.get("bestBid"), lm.get("bestAsk")
                    if isinstance(bid, (int, float)):
                        m["bid"] = round(bid * 100)
                    if isinstance(ask, (int, float)):
                        m["ask"] = round(ask * 100)
                    yes = catalog._poly_yes_cents(lm)
                    if yes is not None:
                        m["yes"] = yes
                # Gamma's quotes lag the matching engine by seconds on fast markets;
                # overwrite with real-time CLOB books (shared ~2s cache per event).
                tok_by_mid = {}
                for lm in r.get("markets") or []:
                    try:
                        toks = json.loads(lm.get("clobTokenIds") or "[]")
                    except (ValueError, TypeError):
                        toks = []
                    if toks and lm.get("id") is not None:
                        tok_by_mid[str(lm["id"])] = str(toks[0])
                if tok_by_mid:
                    live_books = _market_live_books(f"p:{key}", tok_by_mid)
                    for m in markets:
                        bk = live_books.get(str(m.get("id") or ""))
                        if not bk:
                            continue
                        bid, ask = bk
                        if bid is not None:
                            m["bid"] = bid
                        if ask is not None:
                            m["ask"] = ask
                        # A tight book's midpoint is the live price; keep Gamma's
                        # last for wide/thin books where the mid is meaningless.
                        if bid is not None and ask is not None and ask - bid <= 5:
                            m["yes"] = round((bid + ask) / 2)
        elif venue == "kalshi":
            raw = http_get(f"https://api.elections.kalshi.com/v1/events/{key}", ttl=2)
            ev_data = (raw or {}).get("event", {})
            extra_info["description"] = ev_data.get("underlying", "")
            extra_info["rules"] = ev_data.get("settle_details", "")
            sources = ev_data.get("settlement_sources") or []
            extra_info["resolution_source"] = ", ".join(s.get("name") or s.get("url") or "" for s in sources)
            extra_info["end_date"] = ev_data.get("target_datetime")

            live_by_ticker = {lm.get("ticker"): lm for lm in ev_data.get("markets") or []}
            for m in markets:
                lm = live_by_ticker.get(m.get("ticker"))
                if not lm:
                    continue
                m["end_date"] = (lm.get("close_date") or lm.get("expiration_date")
                                 or lm.get("expected_expiration_date") or m["end_date"])
                bid, ask, last = catalog._kalshi_market_prices(lm)
                if bid is not None:
                    m["bid"] = bid
                if ask is not None:
                    m["ask"] = ask
                if last is not None:
                    m["yes"] = last
                elif bid is not None and ask is not None and not (bid == 0 and ask == 100):
                    m["yes"] = round((bid + ask) / 2)
        elif venue == "cryptocom":
            live = _cryptocom_live_contracts(str(ev.get("id") or "")) if ev.get("id") else {}
            for m in markets:
                c = live.get(str(m.get("id") or ""))
                if not c:
                    continue
                yes = catalog._cryptocom_price_cents(c.get("yes"))
                no = catalog._cryptocom_price_cents(c.get("no"))
                if yes is not None:
                    # crypto.com quotes one firm price per side, not a book.
                    m["yes"] = m["bid"] = m["ask"] = yes
                if no is not None:
                    m["no"] = no
    except Exception:
        pass

    # Real-priced outcomes first; unpriced placeholders sink to the bottom.
    # (Sorted after the live-price overlay so fresh quotes drive the order.)
    markets.sort(key=lambda m: -(m["yes"] if m["yes"] is not None else -1))

    event_end_date = extra_info.get("end_date") or ev.get("end_date")

    return {
        "venue": ev.get("source"),
        "title": ev.get("title") or "",
        "subtitle": ev.get("subtitle") or "",
        "category": _explore_category(ev.get("category")),
        "raw_category": (ev.get("category") or "").strip(),
        "volume": int(ev.get("volume") or 0),
        "image": ev.get("image") or "",
        "url": ev.get("url") or "",
        "event_key": key,
        "markets": markets,
        "description": extra_info.get("description", ""),
        "rules": extra_info.get("rules", ""),
        "resolution_source": extra_info.get("resolution_source", ""),
        "end_date": event_end_date,
    }


_explain_cache: dict[str, dict] = {}
_explain_lock = threading.Lock()

_CATEGORY_FACTORS = {
    "politics": ["Polling averages and trend direction", "Endorsements and party support", "Fundraising and campaign spending", "News events and candidate controversies", "Historical base rates for the office"],
    "crypto": ["Spot price and recent momentum", "On-chain metrics (volume, active addresses)", "Regulatory news and macro risk-off sentiment", "Exchange flows and whale positioning", "Options market implied volatility"],
    "sports": ["Team form and recent results", "Head-to-head record", "Injuries and player availability", "Home/away advantage", "Betting market odds from sportsbooks"],
    "economics": ["Official data releases (CPI, NFP, GDP)", "Fed communications and market pricing", "Analyst consensus forecasts", "Recent data revisions", "Macro cross-asset signals"],
    "science": ["Published research and trial results", "Regulatory timelines and approval history", "Expert consensus and pre-registered predictions", "Replication status of prior findings"],
    "entertainment": ["Box office projections and tracking data", "Critic and audience reception", "Studio marketing spend", "Comparable title performance", "Release window competition"],
    "default": ["Recent news and sentiment", "Historical base rate for similar events", "Expert forecasts and consensus", "Market liquidity and trader positioning", "Resolution criteria and edge cases"],
}


def _explain_category_factors(category: str) -> list:
    cat = (category or "").lower()
    for k, v in _CATEGORY_FACTORS.items():
        if k in cat:
            return v
    return _CATEGORY_FACTORS["default"]


def market_explain(venue: str, key: str) -> dict:
    """Build an 'Understanding the Data' section from market fields — no API key needed."""
    cache_key = f"{venue}:{key}"
    with _explain_lock:
        if cache_key in _explain_cache:
            return _explain_cache[cache_key]

    data = market_payload(venue, key)
    if not data:
        return {"error": "market not found"}

    title = data.get("title", "")
    description = (data.get("description") or "").strip()
    rules = (data.get("rules") or "").strip()
    resolution_source = (data.get("resolution_source") or "").strip()
    volume = data.get("volume_usd") or data.get("volume") or 0
    category = data.get("category") or data.get("raw_category") or ""
    markets = data.get("markets") or []

    top = sorted(markets, key=lambda m: m.get("yes") or 0, reverse=True)
    top_label = top[0].get("label", "") if top else ""
    top_pct = top[0].get("yes") if top else None

    if description and len(description) > 40:
        summary = description[:300]
        if len(description) > 300:
            cut = summary.rfind(". ")
            summary = (summary[:cut + 1] if cut > 80 else summary).rstrip()
    else:
        if volume >= 1_000_000:
            vol_str = f"${volume / 1_000_000:.1f}M"
        elif volume >= 1_000:
            vol_str = f"${volume / 1_000:.0f}K"
        else:
            vol_str = f"${int(volume)}"
        vlabel = {"kalshi": "Kalshi", "polymarket": "Polymarket",
                  "cryptocom": "Crypto.com"}.get(venue, venue.capitalize())
        summary = f"This market asks: {title}. Traders have staked {vol_str} in total volume on {vlabel}."
        if top_label and top_pct is not None:
            summary += f" The crowd currently prices \"{top_label}\" at {top_pct}%."

    if top_label and top_pct and top_pct > 0:
        ret = round(100 / top_pct - 1, 1)
        how_to_read = (
            f"A price of {top_pct}¢ for \"{top_label}\" means traders collectively estimate "
            f"a {top_pct}% chance of that outcome. Buying at {top_pct}¢ pays out $1 if it resolves Yes — a {ret}× return on your stake."
        )
    else:
        how_to_read = "Each contract pays $1 if the outcome resolves Yes, so the price in cents equals the market-implied probability in percent. A 60¢ contract implies a 60% chance."

    if resolution_source:
        resolution = f"Resolves based on: {resolution_source.rstrip('.')}."
    elif rules:
        first = rules.split(".")[0].strip()
        resolution = (first + ".") if first else "Resolution follows the rules stated in the market description."
    else:
        resolution = "Resolution criteria are defined on the originating platform — check the market's official rules page for details."

    result = {
        "enabled": True,
        "summary": summary,
        "key_factors": _explain_category_factors(category),
        "how_to_read": how_to_read,
        "resolution": resolution,
    }

    with _explain_lock:
        _explain_cache[cache_key] = result
    return result


def market_activity_payload(venue: str, key: str, outcome: str = "",
                            hours: int = 24, min_usd: float = 500,
                            rollup: bool = True) -> dict | None:
    """Live order book, trades, whale flow for one outcome (+ event rollup)."""
    cache_key = (venue, key, outcome, hours, min_usd, rollup)
    now = time.time()
    hit = _ACTIVITY_CACHE.get(cache_key)
    if hit and now - hit[0] < _ACTIVITY_TTL:
        return hit[1]

    ev = market_payload(venue, key)
    if not ev:
        return None
    markets = ev.get("markets") or []
    if not markets:
        return None
    hours = max(1, min(720, hours))
    min_usd = max(50, min_usd)
    min_ts = int(time.time()) - hours * 3600

    if venue == "cryptocom":
        # crypto.com Predict exposes no public trade/order-book feed — return
        # the event snapshot with empty activity so the market page renders.
        pick = None
        if outcome:
            ol = outcome.lower()
            pick = markets[0]
            for m in markets:
                if m.get("slug") == ol or (m.get("label") or "").lower() == ol:
                    pick = m
                    break
        result = {
            "mode": "outcome" if outcome else "overview",
            "event": ev,
            "outcome": pick,
            "hours": hours,
            "min_usd": min_usd,
            "event_whales": [],
            "history": [],
            "orderbook": {} if outcome else None,
            "recent_trades": [] if outcome else None,
            "whale_trades": [] if outcome else None,
            "whale": None,
        }
        _ACTIVITY_CACHE[cache_key] = (now, result)
        return result

    if not outcome:
        if venue == "kalshi":
            event_whales = market_activity.kalshi_event_whale_rollup(markets, hours, min_usd)
        else:
            cap = min(len(markets), 24)
            event_whales = market_activity.poly_event_whale_rollup(
                markets, min_ts, min_usd, limit=cap)
        result = {
            "mode": "overview",
            "event": ev,
            "outcome": None,
            "hours": hours,
            "min_usd": min_usd,
            "event_whales": event_whales,
            "history": [],
            "orderbook": None,
            "recent_trades": None,
            "whale_trades": None,
            "whale": None,
        }
        _ACTIVITY_CACHE[cache_key] = (now, result)
        return result

    pick = markets[0]
    ol = outcome.lower()
    for m in markets:
        if m.get("slug") == ol or (m.get("label") or "").lower() == ol:
            pick = m
            break
    if venue == "kalshi":
        ticker = pick.get("ticker")
        if not ticker:
            return None
        bundle = market_activity.kalshi_activity_bundle(
            markets, ticker, hours=hours, min_usd=min_usd)
        event_whales = bundle.pop("event_whales")
        history = bundle.pop("history")
        detail = bundle
    else:
        mid = pick.get("id")
        if not mid:
            return None
        from concurrent.futures import ThreadPoolExecutor
        hist_key = ev.get("event_key") or key
        with ThreadPoolExecutor(max_workers=2) as pool:
            bundle_fut = pool.submit(
                market_activity.poly_activity_bundle,
                markets, str(mid), pick.get("slug") or "", hours, min_usd, rollup,
            )
            hist_fut = pool.submit(
                lambda: poly_history(hist_key, top_n=1).get("series", [{}])[0].get("points", []),
            )
            try:
                bundle = bundle_fut.result()
                history = hist_fut.result()
            except Exception:
                bundle = market_activity.poly_activity_bundle(
                    markets, str(mid), pick.get("slug") or "", hours, min_usd, rollup)
                history = []
        meta = bundle.pop("meta", {})
        event_whales = bundle.pop("event_whales")
        pick = {
            **pick,
            "market_id": str(mid),
            "yes_token_id": meta.get("yes_token_id") or "",
            "no_token_id": meta.get("no_token_id") or "",
        }
        detail = {k: v for k, v in bundle.items() if k != "meta"}
    result = {
        "mode": "outcome",
        "event": ev,
        "outcome": pick,
        "hours": hours,
        "min_usd": min_usd,
        "event_whales": event_whales,
        "history": history,
        **detail,
    }
    _ACTIVITY_CACHE[cache_key] = (now, result)
    if len(_ACTIVITY_CACHE) > 200:
        cutoff = now - _ACTIVITY_TTL
        for k in [k for k, (t, _) in _ACTIVITY_CACHE.items() if t < cutoff]:
            _ACTIVITY_CACHE.pop(k, None)
    return result

_CRYPTO_OVERVIEW_CACHE = {"o": None, "at": 0.0}
_CRYPTO_OVERVIEW_TTL = 2
# Single-flight: when the cache expires, exactly one thread recomputes while all other
# clients keep serving the last snapshot. Without this, N clients would stampede the DB
# at the same instant the TTL lapses.
_CRYPTO_OVERVIEW_LOCK = threading.Lock()
# Pre-serialized snapshot (bytes + version tag) shared by all websocket clients so we
# never json.dumps the ~90KB payload once per connection.
_CRYPTO_BYTES_CACHE = {"snap": (b"", b""), "src_at": -1.0}

_CRYPTO_PRICES_CACHE = {"data": None, "at": 0.0, "busy": False}
_CRYPTO_PRICES_TTL = 30

def fetch_binance_prices() -> dict:
    prices = {
        "BTC": {"price": 0.0, "change": 0.0},
        "ETH": {"price": 0.0, "change": 0.0},
        "SOL": {"price": 0.0, "change": 0.0},
        "DOGE": {"price": 0.0, "change": 0.0},
        "XRP": {"price": 0.0, "change": 0.0},
        "LTC": {"price": 0.0, "change": 0.0},
        "HYPE": {"price": 0.0, "change": 0.0},
    }
    try:
        req = Request("https://api.binance.com/api/v3/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            mapping = {
                "BTCUSDT": "BTC",
                "ETHUSDT": "ETH",
                "SOLUSDT": "SOL",
                "DOGEUSDT": "DOGE",
                "XRPUSDT": "XRP",
                "LTCUSDT": "LTC",
            }
            for item in data:
                symbol = item.get("symbol")
                if symbol in mapping:
                    coin = mapping[symbol]
                    prices[coin]["price"] = float(item.get("lastPrice") or 0.0)
                    prices[coin]["change"] = float(item.get("priceChangePercent") or 0.0)
    except Exception as e:
        print("Error fetching Binance prices:", e)

    try:
        if prices["BTC"]["price"] == 0.0:
            req = Request("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,dogecoin,ripple,litecoin,hyperliquid&vs_currencies=usd&include_24hr_change=true", headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=5) as resp:
                cg_data = json.loads(resp.read())
                cg_map = {
                    "bitcoin": "BTC",
                    "ethereum": "ETH",
                    "solana": "SOL",
                    "dogecoin": "DOGE",
                    "ripple": "XRP",
                    "litecoin": "LTC",
                    "hyperliquid": "HYPE",
                }
                for cg_id, coin in cg_map.items():
                    if cg_id in cg_data:
                        prices[coin]["price"] = float(cg_data[cg_id].get("usd") or 0.0)
                        prices[coin]["change"] = float(cg_data[cg_id].get("usd_24h_change") or 0.0)
        else:
            req = Request("https://api.coingecko.com/api/v3/simple/price?ids=hyperliquid&vs_currencies=usd&include_24hr_change=true", headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=5) as resp:
                cg_data = json.loads(resp.read())
                if "hyperliquid" in cg_data:
                    prices["HYPE"]["price"] = float(cg_data["hyperliquid"].get("usd") or 0.0)
                    prices["HYPE"]["change"] = float(cg_data["hyperliquid"].get("usd_24h_change") or 0.0)
    except Exception as e:
        print("Error fetching Hyperliquid/CG price:", e)
        if prices["HYPE"]["price"] == 0.0:
            prices["HYPE"]["price"] = 9.45
            prices["HYPE"]["change"] = 3.25
            
    return prices

def _crypto_prices_cached() -> dict:
    """Spot prices with stale-while-revalidate so the overview never blocks on Binance/CoinGecko."""
    now = time.time()
    cache = _CRYPTO_PRICES_CACHE
    if cache["data"] is None:
        cache["data"] = fetch_binance_prices()
        cache["at"] = time.time()
        return cache["data"]
    if now - cache["at"] >= _CRYPTO_PRICES_TTL and not cache["busy"]:
        cache["busy"] = True

        def _refresh():
            try:
                fresh = fetch_binance_prices()
                prev = cache["data"] or {}
                # a failed fetch returns zeros — keep the last good quote per coin
                for coin, q in fresh.items():
                    if q["price"] == 0.0 and prev.get(coin, {}).get("price"):
                        fresh[coin] = prev[coin]
                cache["data"] = fresh
                cache["at"] = time.time()
            finally:
                cache["busy"] = False

        threading.Thread(target=_refresh, daemon=True).start()
    return cache["data"]


_CRYPTO_ET_TZ = ZoneInfo("America/New_York")
# polymarket slug: btc-updown-5m-1783278600 (window start epoch; 5m/15m/1h/4h)
_UPDOWN_SLUG_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mh])-(\d{9,})")
# kalshi ticker: KXETH15M-26JUL050030 (window close, ET wall clock)
_KALSHI_SHORT_RE = re.compile(r"^KX([A-Z0-9]+?)(\d{1,2})M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
_MONTH_NUM = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _short_window(ev: dict):
    """(coin, start_epoch, end_epoch, freq_label, freq_seconds) for time-slot
    up/down markets (5-min, 15-min, hourly...), else None."""
    if ev.get("source") == "cryptocom":
        s = ev.get("cc_short")
        if s and s.get("coin") and s.get("end"):
            return s["coin"], float(s["start"]), float(s["end"]), s["freq"], int(s["dur"])
        return None
    if ev.get("source") == "polymarket":
        m = _UPDOWN_SLUG_RE.match(ev.get("slug") or "")
        if not m:
            return None
        coin = m.group(1).upper()
        n, unit, start = int(m.group(2)), m.group(3), int(m.group(4))
        dur = n * (3600 if unit == "h" else 60)
        return coin, float(start), float(start + dur), f"{n}{unit}", dur
    if ev.get("source") == "kalshi":
        m = _KALSHI_SHORT_RE.match((ev.get("ticker") or "").upper())
        if not m:
            return None
        mon = _MONTH_NUM.get(m.group(4))
        if not mon:
            return None
        mins = int(m.group(2))
        try:
            close_dt = datetime(2000 + int(m.group(3)), mon, int(m.group(5)),
                                int(m.group(6)), int(m.group(7)), tzinfo=_CRYPTO_ET_TZ)
        except ValueError:
            return None
        end = close_dt.timestamp()
        return m.group(1), end - mins * 60, end, f"{mins}m", mins * 60
    return None


def _crypto_outcomes(ev: dict) -> list[dict]:
    outcomes = []
    for m in (ev.get("markets") or [])[:4]:
        last, bid, ask = m.get("last"), m.get("bid"), m.get("ask")
        # Prefer the live book midpoint: for fast up-down windows Gamma's
        # outcomePrices ("last") lags the order book by many seconds, and a
        # 0/100 book is a no-quote placeholder rather than a real spread.
        price = None
        if bid is not None and ask is not None and not (bid == 0 and ask == 100) \
                and not (bid == 0 and ask == 0):
            price = round((bid + ask) / 2)
        if price is None:
            price = last
        outcomes.append({
            "label": m.get("label") or m.get("yes_subtitle") or m.get("title") or "",
            "yes": price,
            "bid": bid,
            "ask": ask,
            "ticker": m.get("ticker"),
            "id": m.get("id"),
        })
    return outcomes


def classify_crypto_event(ev: dict) -> str:
    title = (ev.get("title") or "").lower()
    ticker = (ev.get("ticker") or "").lower()
    event_id = (ev.get("event_id") or ev.get("id") or "").lower()
    combined = f"{title} {ticker} {event_id}"
    
    if "btc" in combined or "bitcoin" in combined:
        return "BTC"
    if "eth" in combined or "ethereum" in combined:
        return "ETH"
    if "sol" in combined or "solana" in combined:
        return "SOL"
    if "doge" in combined:
        return "DOGE"
    if "xrp" in combined or "ripple" in combined:
        return "XRP"
    if "ltc" in combined or "litecoin" in combined:
        return "LTC"
    if "hype" in combined or "hyperliquid" in combined:
        return "HYPE"
        
    altcoin_keywords = [
        "bnb", "binance", "cardano", "ada", "polkadot", "dot", "avax", "avalanche", 
        "chainlink", "link", "trx", "tron", "shib", "shiba", "ton", "toncoin",
        "near", "sui", "pepe", "wif", "bonk", "render", "rndr", "apt", "aptos",
        "uniswap", "uni", "jup", "jupiter", "ldo", "lido", "maker", "mkr", "aave"
    ]
    if any(k in combined for k in altcoin_keywords) or "altcoin" in combined:
        return "Altcoins"
        
    return "Macro"

def _crypto_news_headlines(events: list[dict], limit: int = 8) -> list[dict]:
    # Filter out short-term 5-min markets for headlines, keep high volume macro/event markets
    candidates = [e for e in events if not e.get("is_short") and e.get("volume", 0) > 1000]
    candidates.sort(key=lambda e: -e.get("volume", 0))
    headlines = []
    for c in candidates[:limit]:
        headlines.append({
            "title": c["title"],
            "url": c["url"] or f"/explore?q={c['title']}",
            "source": c["source"],
            "volume": c["volume"],
            "yes": c["yes"],
        })
    return headlines

_LIVE_COIN_ORDER = {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3, "DOGE": 4,
                    "LTC": 5, "HYPE": 6, "BNB": 7}
_CRYPTO_EVENTS_CAP = 150


def crypto_overview() -> dict:
    _crypto_live_touch()
    now = time.time()
    hit = _CRYPTO_OVERVIEW_CACHE.get("o")
    if hit and now - hit["at"] < _CRYPTO_OVERVIEW_TTL:
        return hit["data"]

    # Cache expired. Only one thread does the (DB + classification) work; concurrent
    # callers keep serving the previous snapshot until it's ready — no stampede.
    if hit is not None and not _CRYPTO_OVERVIEW_LOCK.acquire(blocking=False):
        return hit["data"]
    if hit is None:
        _CRYPTO_OVERVIEW_LOCK.acquire()   # first-ever call must block to populate
    try:
        now = time.time()
        cached = _CRYPTO_OVERVIEW_CACHE.get("o")
        if cached and now - cached["at"] < _CRYPTO_OVERVIEW_TTL:
            return cached["data"]         # another thread already refreshed it
        return _crypto_overview_build(now)
    finally:
        _CRYPTO_OVERVIEW_LOCK.release()


def _crypto_overview_build(now: float) -> dict:
    prices = _crypto_prices_cached()
    events = catalog_db.load_crypto_events()

    classified_events = []
    short_candidates = []

    coin_counts = {
        "BTC": {"live": 0, "total": 0},
        "ETH": {"live": 0, "total": 0},
        "SOL": {"live": 0, "total": 0},
        "DOGE": {"live": 0, "total": 0},
        "XRP": {"live": 0, "total": 0},
        "LTC": {"live": 0, "total": 0},
        "HYPE": {"live": 0, "total": 0},
        "Altcoins": {"live": 0, "total": 0},
        "Macro": {"live": 0, "total": 0},
    }

    for ev in events:
        title = ev.get("title", "")
        win = _short_window(ev)
        if win:
            coin_sym, start, end, freq, freq_s = win
            # time-slot market: keep only windows that are live now or upcoming soon
            if end <= now or start > now + 4 * 3600:
                continue
            outs = _crypto_outcomes(ev)
            # Kalshi / crypto.com windows settle over-or-under a fixed strike carried
            # in the outcome label ("Target Price: $64,074.23"); Polymarket up/down
            # windows settle vs the window's open price and have no fixed strike.
            strike = None
            if ev.get("source") != "polymarket":
                for o in outs:
                    strike = _parse_target_price(o.get("label"))
                    if strike is not None:
                        break
            short_candidates.append({
                "id": str(ev.get("id") or ev.get("ticker") or ev.get("slug") or ""),
                "source": ev.get("source"),
                "coin": coin_sym,
                "title": title,
                "freq": freq,
                "freq_s": freq_s,
                "start": start,
                "end": end,
                "volume": int(ev.get("volume") or 0),
                "url": ev.get("url") or "",
                "kind": "updown" if ev.get("source") == "polymarket" else "target",
                "strike": strike,
                "outcomes": outs,
            })
            continue
        # unparseable time-slot leftovers: drop rather than clutter the browse list
        if " - " in title and "up or down" in title.lower():
            continue

        coin = classify_crypto_event(ev)
        outcomes = _crypto_outcomes(ev)
        classified_events.append({
            "id": str(ev.get("id") or ev.get("ticker") or ev.get("slug") or ""),
            "source": ev.get("source"),
            "title": title,
            "subtitle": ev.get("subtitle") or "",
            "volume": int(ev.get("volume") or 0),
            "url": ev.get("url") or "",
            "category": coin,
            "outcomes": outcomes,
            "yes": outcomes[0]["yes"] if outcomes else None,
        })
        if coin in coin_counts:
            coin_counts[coin]["total"] += 1

    # keep the current window plus the next upcoming one per (coin, cadence, venue)
    groups: dict[tuple, list] = {}
    for c in short_candidates:
        groups.setdefault((c["coin"], c["freq"], c["source"]), []).append(c)
    live = []
    for lst in groups.values():
        lst.sort(key=lambda x: x["start"])
        cur = next((x for x in lst if x["start"] <= now < x["end"]), None)
        nxt = next((x for x in lst if x["start"] > now), None)
        if cur:
            cur["status"] = "live"
            live.append(cur)
        if nxt and (cur is None or nxt["start"] - now <= 900):
            nxt["status"] = "next"
            live.append(nxt)

    # Sort by time-to-expiry (soonest-closing window first); ties broken by coin
    # order then venue so equal-expiry cards stay stably grouped.
    live.sort(key=lambda x: (x["end"], _LIVE_COIN_ORDER.get(x["coin"], 99),
                             x["source"] != "polymarket"))

    live_now_total = 0
    for x in live:
        if x["status"] == "live":
            live_now_total += 1
            if x["coin"] in coin_counts:
                coin_counts[x["coin"]]["live"] += 1

    coins_list = [
        {"key": "BTC", "name": "Bitcoin", "symbol": "BTC", "emoji": "₿", "accent": "#f59e0b", **prices["BTC"], **coin_counts["BTC"]},
        {"key": "ETH", "name": "Ethereum", "symbol": "ETH", "emoji": "Ξ", "accent": "#627eea", **prices["ETH"], **coin_counts["ETH"]},
        {"key": "SOL", "name": "Solana", "symbol": "SOL", "emoji": "◎", "accent": "#14f195", **prices["SOL"], **coin_counts["SOL"]},
        {"key": "DOGE", "name": "Dogecoin", "symbol": "DOGE", "emoji": "Ð", "accent": "#c2a633", **prices["DOGE"], **coin_counts["DOGE"]},
        {"key": "XRP", "name": "XRP", "symbol": "XRP", "emoji": "✕", "accent": "#23292f", **prices["XRP"], **coin_counts["XRP"]},
        {"key": "LTC", "name": "Litecoin", "symbol": "LTC", "emoji": "Ł", "accent": "#345d9d", **prices["LTC"], **coin_counts["LTC"]},
        {"key": "HYPE", "name": "Hyperliquid", "symbol": "HYPE", "emoji": "⚡", "accent": "#00f2fe", **prices["HYPE"], **coin_counts["HYPE"]},
        {"key": "Altcoins", "name": "Altcoins", "symbol": "ALT", "emoji": "🪙", "accent": "#10b981", "price": 0.0, "change": 0.0, **coin_counts["Altcoins"]},
        {"key": "Macro", "name": "Macro & Policy", "symbol": "MACRO", "emoji": "🌐", "accent": "#3b82f6", "price": 0.0, "change": 0.0, **coin_counts["Macro"]},
    ]
    
    data = {
        "type": "crypto",
        "coins": coins_list,
        "live": live,
        "live_total": live_now_total,
        "events": classified_events[:_CRYPTO_EVENTS_CAP],
        "total_count": len(classified_events),
        "updated_at": now,
        "live_refresh": dict(_CRYPTO_LIVE_STATS),
        "news": _crypto_news_headlines(classified_events),
    }

    _CRYPTO_OVERVIEW_CACHE["o"] = {"data": data, "at": now}
    return data


def crypto_overview_bytes() -> tuple[bytes, bytes]:
    """Shared, pre-serialized (bytes, version) for the crypto websocket.

    Serializing the ~90KB overview once per refresh — instead of once per connected
    client per tick — is what keeps the socket cheap under thousands of viewers. The
    version tag lets each connection skip pushes when nothing changed.
    """
    data = crypto_overview()
    src_at = (_CRYPTO_OVERVIEW_CACHE.get("o") or {}).get("at", 0.0)
    cache = _CRYPTO_BYTES_CACHE
    if cache["src_at"] != src_at:
        raw = json.dumps(data).encode()
        # The version tag hashes the market content *without* the wall-clock `updated_at`
        # stamp, so a rebuild that produced identical prices/markets yields the same
        # version — and every connected client skips the push. That's what stops a
        # 90KB-per-user broadcast on every tick when nothing actually moved.
        content = {k: v for k, v in data.items() if k not in ("updated_at", "live_refresh")}
        ver = hashlib.md5(json.dumps(content, sort_keys=True).encode()).digest()
        # Assign the (bytes, ver) pair as one tuple so concurrent readers never see a
        # mismatched bytes/version split.
        cache["snap"] = (raw, ver)
        cache["src_at"] = src_at
    return cache["snap"]


# ------------------------------------------------------------- crypto market series
# One 5-minute market is really an endless series of time-slot windows (…1:45, …1:50).
# The detail page shows the live window plus its schedule and recent settled windows.
_CRYPTO_COIN_INFO = {
    "BTC":  {"name": "Bitcoin",     "emoji": "₿", "accent": "#f59e0b"},
    "ETH":  {"name": "Ethereum",    "emoji": "Ξ", "accent": "#627eea"},
    "SOL":  {"name": "Solana",      "emoji": "◎", "accent": "#14f195"},
    "DOGE": {"name": "Dogecoin",    "emoji": "Ð", "accent": "#c2a633"},
    "XRP":  {"name": "XRP",         "emoji": "✕", "accent": "#3b82f6"},
    "LTC":  {"name": "Litecoin",    "emoji": "Ł", "accent": "#345d9d"},
    "HYPE": {"name": "Hyperliquid", "emoji": "⚡", "accent": "#00d3c5"},
    "BNB":  {"name": "BNB",         "emoji": "🔶", "accent": "#f0b90b"},
    "NEAR": {"name": "NEAR",        "emoji": "Ⓝ", "accent": "#111827"},
    "ZEC":  {"name": "Zcash",       "emoji": "ⓩ", "accent": "#f4b728"},
}
_CRYPTO_SERIES_CACHE: dict[tuple, dict] = {}
_CRYPTO_SERIES_TTL = 2
_PRICE_IN_LABEL_RE = re.compile(r"\$([0-9][0-9,]*\.?[0-9]*)")


def _parse_target_price(label: str):
    m = _PRICE_IN_LABEL_RE.search(label or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def crypto_series(coin: str, freq: str, source: str) -> dict:
    """Live window + schedule + recent windows for a single short-term crypto market."""
    _crypto_live_touch()
    coin = (coin or "").upper()
    freq = (freq or "").lower()
    source = (source or "").lower()
    key = (coin, freq, source)

    now = time.time()
    hit = _CRYPTO_SERIES_CACHE.get(key)
    if hit and now - hit["at"] < _CRYPTO_SERIES_TTL:
        # Serve the cached body but re-stamp statuses against the live clock so the
        # current/past/upcoming split is always correct even between rebuilds.
        return _series_restamp(hit["data"], now)

    windows = []
    for ev in catalog_db.load_crypto_events():
        if (ev.get("source") or "").lower() != source:
            continue
        win = _short_window(ev)
        if not win:
            continue
        c, start, end, f, _fs = win
        if c != coin or f != freq:
            continue
        outcomes = _crypto_outcomes(ev)
        target = _parse_target_price(outcomes[0]["label"]) if (source == "kalshi" and outcomes) else None
        windows.append({
            "id": str(ev.get("id") or ev.get("ticker") or ev.get("slug") or ""),
            "start": start,
            "end": end,
            "volume": int(ev.get("volume") or 0),
            "url": ev.get("url") or "",
            "title": ev.get("title", ""),
            "yes": outcomes[0]["yes"] if outcomes else None,
            "bid": outcomes[0]["bid"] if outcomes else None,
            "ask": outcomes[0]["ask"] if outcomes else None,
            "target": target,
        })

    windows.sort(key=lambda w: w["start"])
    dur = int(windows[0]["end"] - windows[0]["start"]) if windows else 0

    # For Kalshi the catalog only holds the currently active market per series;
    # future windows exist on the exchange but aren't pre-listed.  Synthesise the
    # upcoming time-slot schedule so the detail page isn't blank after the live
    # window.  Prices are unknown ("target": None, "yes": None) — the frontend
    # shows "—" and fills in real prices when the catalog refreshes.
    if source == "kalshi" and dur > 0:
        known_starts = {w["start"] for w in windows}
        # Align to the nearest ET boundary (Kalshi windows snap to :00/:15/:30/:45)
        dur_m = dur // 60
        et_now = datetime.fromtimestamp(now, tz=_CRYPTO_ET_TZ)
        boundary_mins = (et_now.hour * 60 + et_now.minute) // dur_m * dur_m
        boundary_dt = et_now.replace(hour=boundary_mins // 60,
                                     minute=boundary_mins % 60,
                                     second=0, microsecond=0)
        boundary_epoch = boundary_dt.timestamp()
        for i in range(20):
            slot_start = boundary_epoch + i * dur
            if slot_start > now + 4 * 3600:
                break
            if slot_start not in known_starts:
                known_starts.add(slot_start)
                windows.append({
                    "id": f"syn-{coin}-{freq}-{int(slot_start)}",
                    "start": slot_start,
                    "end": slot_start + dur,
                    "volume": 0,
                    "url": "",
                    "title": "",
                    "yes": None,
                    "bid": None,
                    "ask": None,
                    "target": None,
                    "synthetic": True,
                })
        windows.sort(key=lambda w: w["start"])

    # Trim to a focused slice: recent settled windows, the live one, and the next few
    # upcoming — the schedule stretches days out otherwise (100+ windows) and bloats the
    # payload. Re-stamping happens against the live clock, so this keeps the current
    # window even as time advances within the cache TTL.
    past = [w for w in windows if w["end"] <= now]
    live_or_next = [w for w in windows if w["end"] > now]
    windows = past[-8:] + live_or_next[:20]

    info = _CRYPTO_COIN_INFO.get(coin, {"name": coin, "emoji": "🪙", "accent": "#10b981"})
    prices = _crypto_prices_cached()
    spot = prices.get(coin)   # {"price","change"} for the 7 majors, else None

    data = {
        "type": "crypto_series",
        "coin": coin,
        "freq": freq,
        "source": source,
        "kind": "updown" if source == "polymarket" else "target",
        "name": info["name"],
        "emoji": info["emoji"],
        "accent": info["accent"],
        "spot": spot,
        "dur": dur,
        "windows": windows,       # full schedule; client slices recent/current/upcoming
        "updated_at": now,
    }
    _CRYPTO_SERIES_CACHE[key] = {"data": data, "at": now}
    return _series_restamp(data, now)


def _series_restamp(data: dict, now: float) -> dict:
    """Attach clock-relative status + current-window pointer without recomputing."""
    current = None
    live_total = 0
    for w in data.get("windows") or []:
        if w["end"] <= now:
            w["status"] = "past"
        elif w["start"] <= now < w["end"]:
            w["status"] = "live"
            current = w
            live_total += 1
        else:
            w["status"] = "upcoming"
    data["current_id"] = current["id"] if current else None
    data["is_live"] = live_total > 0
    data["server_now"] = now
    return data


# ------------------------------------------------------------- crypto live refresher
# The full catalog refresh (~5 min) is far too slow for 5-/15-minute up-down windows:
# their prices freeze at the pre-listing 50/50 and never move on the page. This
# background loop re-fetches ONLY the short-term crypto markets every few seconds —
# one batched Gamma request for every current+next Polymarket window, plus one small
# Kalshi request per short series — and upserts them into the catalog DB, so every
# reader (overview, series page, explore, search) serves live odds.
CRYPTO_LIVE_INTERVAL = float(os.environ.get("CRYPTO_LIVE_INTERVAL", "2"))
# Kalshi needs one request per short series per tick (~9), so it runs on a gentler
# cadence than the single batched Polymarket request.
CRYPTO_LIVE_KALSHI_INTERVAL = float(os.environ.get("CRYPTO_LIVE_KALSHI_INTERVAL", "4"))
_CRYPTO_LIVE_IDLE_S = 120     # stop hitting the venues when nobody is watching
_CRYPTO_LIVE_DEMAND = {"at": 0.0}
# Last-tick observability (surfaced as `live_refresh` in the overview payload).
_CRYPTO_LIVE_STATS = {"poly_at": 0.0, "poly_n": 0, "kalshi_at": 0.0, "kalshi_n": 0}
_CRYPTO_LIVE_TARGETS = {"poly": [], "kalshi": [], "at": 0.0}
_CRYPTO_LIVE_TARGETS_TTL = 300
_KALSHI_V2_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"
_FREQ_RE = re.compile(r"^(\d+)([mh])$")


def _crypto_live_touch() -> None:
    _CRYPTO_LIVE_DEMAND["at"] = time.time()


def _freq_seconds(freq: str) -> int:
    m = _FREQ_RE.match(freq or "")
    if not m:
        return 0
    return int(m.group(1)) * (3600 if m.group(2) == "h" else 60)


def _crypto_live_targets(now: float) -> tuple[list, list]:
    """Distinct (coin_slug, freq) Polymarket combos + Kalshi short series tickers,
    rescanned from the catalog every few minutes."""
    t = _CRYPTO_LIVE_TARGETS
    if now - t["at"] < _CRYPTO_LIVE_TARGETS_TTL and (t["poly"] or t["kalshi"]):
        return t["poly"], t["kalshi"]
    poly: set[tuple[str, str]] = set()
    kalshi: set[str] = set()
    try:
        for ev in catalog_db.load_crypto_events():
            if ev.get("source") == "polymarket":
                m = _UPDOWN_SLUG_RE.match(ev.get("slug") or "")
                if m:
                    poly.add((m.group(1), f"{m.group(2)}{m.group(3)}"))
            elif ev.get("source") == "kalshi":
                tk = (ev.get("ticker") or "").upper()
                if _KALSHI_SHORT_RE.match(tk):
                    kalshi.add(tk.split("-")[0])
    except Exception as e:
        print("crypto live target scan error:", e)
    # Always union in the known series: a cold or partially-streamed catalog would
    # otherwise leave a skimpy target list cached for 5 minutes. Polling a series
    # that turns out to be dead just returns no markets and is skipped.
    poly |= {(c, f) for c in ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
             for f in ("5m", "15m")}
    kalshi |= {f"KX{c}15M" for c in ("BTC", "ETH", "SOL", "XRP", "DOGE",
                                     "BNB", "HYPE", "NEAR", "ZEC")}
    t["poly"], t["kalshi"], t["at"] = sorted(poly), sorted(kalshi), now
    return t["poly"], t["kalshi"]


# The bulk fetchers (catalog stream, arb-scan book prefetch) drain the shared
# per-host token buckets for seconds-to-minutes at a time, which starved this
# refresher when it queued on the same budgets. Each venue's live poll runs on its
# own small dedicated bucket instead (~0.5 req/s to Gamma, ~3 req/s to Kalshi);
# a 429 still backs off the whole process via the shared bucket.
_CRYPTO_LIVE_POLY_BUCKET = rate_limit.TokenBucket(
    float(os.environ.get("CRYPTO_LIVE_RATE", "3")), 6.0)
_CRYPTO_LIVE_KALSHI_BUCKET = rate_limit.TokenBucket(
    float(os.environ.get("CRYPTO_LIVE_KALSHI_RATE", "3")), 6.0)


def _crypto_live_get(url: str, bucket: rate_limit.TokenBucket):
    bucket.acquire()
    req = Request(url, headers={"Accept": "application/json",
                                "User-Agent": "kalshi-poly-search/1.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        if e.code == 429:
            time.sleep(rate_limit.note_429(url, e.headers.get("Retry-After")))
            return None
        raise


def _best_clob_level(levels, side: str) -> int | None:
    prices = []
    for lv in levels:
        p = lv.get("price") if isinstance(lv, dict) else None
        try:
            prices.append(round(float(p) * 100))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


def _crypto_live_clob_books(token_ids: list[str]) -> dict[str, tuple]:
    """yes-token id -> (bid_cents, ask_cents) via batched CLOB book requests.

    Gamma's *batched* events endpoint serves books minutes stale, and even its
    single-slug reads lag by many seconds on fast windows; the CLOB books
    endpoint is the matching engine's real-time truth and takes one POST for
    ~100 tokens."""
    out: dict[str, tuple] = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        _CRYPTO_LIVE_POLY_BUCKET.acquire()
        body = json.dumps([{"token_id": t} for t in chunk]).encode()
        req = Request("https://clob.polymarket.com/books", data=body,
                      headers={"Content-Type": "application/json",
                               "Accept": "application/json",
                               "User-Agent": "kalshi-poly-search/1.0"})
        try:
            with urlopen(req, timeout=15) as resp:
                books = json.loads(resp.read())
        except Exception:
            return out
        for b in books if isinstance(books, list) else []:
            tid = str(b.get("asset_id") or "")
            bid = _best_clob_level(b.get("bids") or [], "bid")
            ask = _best_clob_level(b.get("asks") or [], "ask")
            if tid and (bid is not None or ask is not None):
                out[tid] = (bid, ask)
    return out


# Per-event live books for the market detail page, shared across viewers so N
# people polling one market cost one CLOB request per 2s.
_MARKET_BOOKS_CACHE: dict[str, tuple[float, dict]] = {}


def _market_live_books(key: str, tok_by_mid: dict[str, str]) -> dict[str, tuple]:
    """market id -> (bid_cents, ask_cents) for one event, cached ~2s."""
    now = time.time()
    hit = _MARKET_BOOKS_CACHE.get(key)
    if hit and now - hit[0] < 2:
        return hit[1]
    if len(_MARKET_BOOKS_CACHE) > 500:
        _MARKET_BOOKS_CACHE.clear()
    books = _crypto_live_clob_books(sorted(set(tok_by_mid.values())))
    out = {mid: books[tid] for mid, tid in tok_by_mid.items() if tid in books}
    _MARKET_BOOKS_CACHE[key] = (now, out)
    return out


def _crypto_live_poll_poly(combos: list, now: float) -> int:
    """Refresh the current + next window of every combo: one batched Gamma fetch
    for discovery (event/market ids, titles, token ids), then one batched CLOB
    books fetch for live prices, which overwrite the (stale) Gamma quotes."""
    slugs = []
    for coin, freq in combos:
        dur = _freq_seconds(freq)
        if not dur:
            continue
        start = int(now // dur) * dur
        slugs.append(f"{coin}-updown-{freq}-{start}")
        slugs.append(f"{coin}-updown-{freq}-{start + dur}")
    fresh = []
    tok_by_mid: dict[str, str] = {}
    chunk = 25
    for i in range(0, len(slugs), chunk):
        part = slugs[i:i + chunk]
        qs = "&".join(f"slug={s}" for s in part)
        data = _crypto_live_get(f"{catalog.POLY_EVENTS}?limit={len(part)}&{qs}",
                                _CRYPTO_LIVE_POLY_BUCKET)
        for ev in data if isinstance(data, list) else []:
            norm = catalog.normalize_poly_event(ev)
            if not norm:
                continue
            fresh.append(norm)
            for m in ev.get("markets") or []:
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                except (ValueError, TypeError):
                    toks = []
                if toks and m.get("id") is not None:
                    tok_by_mid[str(m["id"])] = str(toks[0])
    if fresh and tok_by_mid:
        books = _crypto_live_clob_books(sorted(set(tok_by_mid.values())))
        for norm in fresh:
            for m in norm.get("markets") or []:
                tid = tok_by_mid.get(str(m.get("id")))
                bk = books.get(tid) if tid else None
                if not bk:
                    continue
                bid, ask = bk
                if bid is not None:
                    m["bid"] = bid
                if ask is not None:
                    m["ask"] = ask
    if fresh:
        catalog_db.upsert_batch("polymarket", fresh)
    _CRYPTO_LIVE_STATS["poly_at"] = time.time()
    _CRYPTO_LIVE_STATS["poly_n"] = len(fresh)
    return len(fresh)


def _crypto_live_poll_kalshi(series_list: list) -> int:
    """Refresh each Kalshi short series' open market(s) via the v2 markets API."""
    fresh = []
    for series in series_list:
        try:
            data = _crypto_live_get(
                f"{_KALSHI_V2_MARKETS}?series_ticker={series}&status=open&limit=20",
                _CRYPTO_LIVE_KALSHI_BUCKET)
        except Exception:
            continue
        by_event: dict[str, list] = {}
        for m in (data or {}).get("markets") or []:
            et = m.get("event_ticker")
            if not et:
                continue
            m = dict(m)
            # normalize_kalshi_event reads the v1 field names for close dates
            m.setdefault("close_date", m.get("close_time"))
            by_event.setdefault(et, []).append(m)
        for et, ms in by_event.items():
            vol = sum(int(float(m.get("volume_fp") or m.get("volume") or 0)) for m in ms)
            norm = catalog.normalize_kalshi_event({
                "event_ticker": et,
                "event_title": ms[0].get("title") or "",
                "sub_title": ms[0].get("subtitle") or "",
                "total_volume": vol * 100,
                "markets": ms,
            })
            if norm:
                fresh.append(norm)
    if fresh:
        catalog_db.upsert_batch("kalshi", fresh)
    _CRYPTO_LIVE_STATS["kalshi_at"] = time.time()
    _CRYPTO_LIVE_STATS["kalshi_n"] = len(fresh)
    return len(fresh)


def start_crypto_live_refresh() -> None:
    if CRYPTO_LIVE_INTERVAL <= 0:
        return

    # One loop per venue: a slow pass on one venue (e.g. Kalshi queueing behind a
    # cold-start catalog stream) must never stall the other venue's live prices.
    def _loop(venue: str, poll, interval: float):
        while True:
            try:
                if time.time() - _CRYPTO_LIVE_DEMAND["at"] > _CRYPTO_LIVE_IDLE_S:
                    time.sleep(1)     # idle: nobody has asked for crypto data lately
                    continue
                poll(time.time())
            except Exception as e:
                print(f"crypto live refresh error ({venue}):", e)
            time.sleep(interval)

    def _poll_poly(now: float):
        combos, _ = _crypto_live_targets(now)
        _crypto_live_poll_poly(combos, now)

    def _poll_kalshi(now: float):
        _, kseries = _crypto_live_targets(now)
        _crypto_live_poll_kalshi(kseries)

    print("crypto live refresher: started")
    threading.Thread(target=_loop, args=("poly", _poll_poly, CRYPTO_LIVE_INTERVAL),
                     daemon=True, name="crypto-live-poly").start()
    threading.Thread(target=_loop, args=("kalshi", _poll_kalshi, CRYPTO_LIVE_KALSHI_INTERVAL),
                     daemon=True, name="crypto-live-kalshi").start()


def poly_history(slug: str, top_n: int = 3, slugs: list[str] | None = None) -> dict:
    """Probability-over-time series for a Polymarket event's leading outcomes,
    pulled live from the CLOB prices-history API."""
    ev_list = http_get(f"https://gamma-api.polymarket.com/events?slug={slug}", ttl=60)
    if isinstance(ev_list, list):
        ev = ev_list[0] if ev_list else None
    elif isinstance(ev_list, dict):
        data = ev_list.get("data") or []
        ev = data[0] if data else None
    else:
        ev = None
    if not ev:
        return {"series": []}
    candidates = []
    for m in ev.get("markets") or []:
        if m.get("closed"):
            continue
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except (ValueError, TypeError):
            toks = []
        if not toks:
            continue
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            cur = float(prices[0]) if prices else 0.0
        except (ValueError, TypeError):
            cur = 0.0
        label = m.get("groupItemTitle") or m.get("question") or ""
        candidates.append((cur, label, toks[0]))
    candidates.sort(key=lambda x: -x[0])
    if slugs:
        want = {s.lower() for s in slugs}
        picked = [(c, l, t) for c, l, t in candidates
                  if market_activity.outcome_slug(l) in want or l.lower() in want]
        top = picked if picked else candidates[:top_n]
    else:
        top = candidates[:top_n]

    def fetch(item):
        _cur, label, token = item
        try:
            h = http_get(f"https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=720", ttl=300)
            pts = [{"t": int(p["t"]), "p": round(float(p["p"]) * 100, 1)}
                   for p in (h.get("history") or [])]
        except Exception:
            pts = []
        return {"label": label, "points": pts}

    series = []
    if top:
        with ThreadPoolExecutor(max_workers=min(6, len(top))) as ex:
            for r in ex.map(fetch, top):
                if r["points"]:
                    series.append(r)
    return {"series": series}


KALSHI_CANDLES = "https://api.elections.kalshi.com/trade-api/v2/markets/candlesticks"


def _kalshi_cents(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return round(f * 100 if f <= 1 else f, 1)
    except (ValueError, TypeError):
        return None



def explore_crypto(q: str = "", limit: int = 48, offset: int = 0, venue: str = "all") -> dict:
    """Crypto-only explore from catalog."""
    events = catalog_db.load_crypto_events()
    rows = []
    for ev in events:
        if venue != "all" and (ev.get("source") or "") != venue:
            continue
        title = ev.get("title") or ""
        if q and q.lower() not in title.lower():
            continue
        markets = ev.get("markets") or []
        yes = None
        for m in markets:
            if m.get("last") is not None:
                yes = m["last"]
                break
        rows.append({
            "venue": ev.get("source"),
            "title": title,
            "subtitle": ev.get("subtitle") or "",
            "category": classify_crypto_event(ev),
            "volume": int(ev.get("volume") or 0),
            "markets": len(markets),
            "yes": yes,
            "url": ev.get("url") or "",
            "detail": f"/{ev.get('source')}/{ev.get('ticker') or ev.get('slug') or ev.get('id')}",
        })
    rows.sort(key=lambda r: -r["volume"])
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "results": page,
        "stats": {"total": total, "shown": len(page), "offset": offset, "limit": limit, "catalog": catalog.cache_stats()},
    }
