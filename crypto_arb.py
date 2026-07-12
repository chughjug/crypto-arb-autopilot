"""Cross-venue arbitrage tracker for short-term crypto threshold markets.

Three venues quote the same short-term question — "will <coin> be above $X at time T?":
  • crypto.com Predict — timed "> $STRIKE" ladders.
  • Kalshi — "$X or above" ladders + 15-min "Target Price: $X" up/down markets.
  • Polymarket — 5-min up/down markets; "Up" uses priceToBeat metadata when
    available, otherwise a recorded T0 tick from Polymarket's RTDS Chainlink stream.

General arbitrage formula (a binary that pays $1 to exactly one of YES/NO):
    edge = $1 − ( cheapest YES ask + cheapest NO ask )   over all venues quoting
                                                          the same coin+expiry+strike
An arbitrage exists when edge > 0: buy YES on the venue where it's cheapest and NO on
the venue where it's cheapest; whichever way the market resolves, one leg pays $1, so
any combined cost below $1 is locked profit. This generalizes to N venues — you simply
take the minimum YES and minimum NO across all of them.

A background poller refreshes a shared snapshot; the page polls it every second and a
cumulative counter tracks distinct opportunities seen.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import catalog          # reuse the curl-based, Akamai-bypassing crypto.com fetcher
import poly_chainlink
import rate_limit

_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("CRYPTO_ARB_WORKERS", "10")),
                           thread_name_prefix="carb")


def _fanout(fn) -> list[dict]:
    """Run fn(coin, cfg) for every coin in parallel and flatten the quote lists."""
    out: list[dict] = []
    for res in _POOL.map(lambda kv: fn(*kv), COINS.items()):
        out.extend(res)
    return out

_ET = ZoneInfo("America/New_York")

ARB_FORMULA = "edge = $1 − ( min YES ask + min NO ask )  across venues, same coin · expiry · strike"

# Coins to track. `kalshi` = search-feed query term; `poly` = Polymarket slug symbol.
# crypto.com uses the dict key as its event_kind. A coin need not exist on every venue —
# missing venues just contribute nothing, and a match needs any two of the three.
COINS = {
    "BTC":  {"kalshi": "bitcoin",     "poly": "btc",  "name": "Bitcoin"},
    "ETH":  {"kalshi": "ethereum",    "poly": "eth",  "name": "Ethereum"},
    "SOL":  {"kalshi": "solana",      "poly": "sol",  "name": "Solana"},
    "XRP":  {"kalshi": "xrp",         "poly": "xrp",  "name": "XRP"},
    "DOGE": {"kalshi": "dogecoin",    "poly": "doge", "name": "Dogecoin"},
    "BNB":  {"kalshi": "bnb",         "poly": "bnb",  "name": "BNB"},
    "HYPE": {"kalshi": "hyperliquid", "poly": "hype", "name": "Hyperliquid"},
    "ADA":  {"kalshi": "cardano",     "poly": "ada",  "name": "Cardano"},
    "LINK": {"kalshi": "chainlink",   "poly": "link", "name": "Chainlink"},
    "LTC":  {"kalshi": "litecoin",    "poly": "ltc",  "name": "Litecoin"},
    "AVAX": {"kalshi": "avalanche",   "poly": "avax", "name": "Avalanche"},
    "BCH":  {"kalshi": "bitcoin cash", "poly": "bch", "name": "Bitcoin Cash"},
}
VENUES = ("cryptocom", "kalshi", "polymarket")
VENUE_LABEL = {"cryptocom": "crypto.com", "kalshi": "Kalshi", "polymarket": "Polymarket"}

_KALSHI_SHORT_SERIES = re.compile(r"^KX([A-Z]+?)(D|15M|5M|1H)?$")
_KALSHI_MAX_HORIZON = float(os.environ.get("CRYPTO_ARB_MAX_HORIZON_H", "26")) * 3600
STRIKE_TOL = float(os.environ.get("CRYPTO_ARB_STRIKE_TOL", "0.0"))
STRIKE_TOL_FRAC = float(os.environ.get("CRYPTO_ARB_STRIKE_TOL_FRAC", "0.00005"))
POLL_SECONDS = float(os.environ.get("CRYPTO_ARB_POLL_SECONDS", "2.0"))

KALSHI_SEARCH = "https://api.elections.kalshi.com/v1/search/series"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CRYPTOCOM_TICKERS = "https://api.crypto.com/exchange/v1/public/get-tickers"

_CC_STRIKE_RE = re.compile(r"[-+]?\$?\s*([\d,]+(?:\.\d+)?)")
_UPDOWN_SLUG_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mh])-(\d+)")
# Dedicated per-host buckets so this poller doesn't starve the catalog's budgets.
_KALSHI_BUCKET = rate_limit.TokenBucket(float(os.environ.get("CRYPTO_ARB_KALSHI_RATE", "5")), 10.0)
_POLY_BUCKET = rate_limit.TokenBucket(float(os.environ.get("CRYPTO_ARB_POLY_RATE", "5")), 10.0)

CENT = Decimal("0.01")


def price_decimal(value) -> Decimal | None:
    """Parse a venue/API price without rounding."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize_strike(value) -> float | None:
    """Strike thresholds are USD — exact to the cent."""
    dec = price_decimal(value)
    if dec is None:
        return None
    return float(dec.quantize(CENT, rounding=ROUND_HALF_UP))


def format_price(value) -> str:
    """Display a spot/settlement price at full precision (no rounding)."""
    dec = price_decimal(value)
    if dec is None:
        return "—"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_strike(value) -> str:
    """Display a strike normalized to cents."""
    dec = price_decimal(normalize_strike(value))
    if dec is None:
        return "—"
    return format(dec.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def threshold_won(price, strike, operator: str) -> bool:
    """Settle using unrounded spot vs cent-exact strike — no float rounding."""
    spot = price_decimal(price)
    threshold = price_decimal(normalize_strike(strike))
    if spot is None or threshold is None or not operator:
        raise ValueError(f"Invalid settlement inputs: price={price!r} strike={strike!r} op={operator!r}")
    if operator == ">":
        return spot > threshold
    if operator == ">=":
        return spot >= threshold
    if operator == "<":
        return spot < threshold
    if operator == "<=":
        return spot <= threshold
    raise ValueError(f"Unsupported settlement operator: {operator}")


def _strike_match(a: float, b: float) -> bool:
    """Cluster strikes within a fractional tolerance (venue ladders rarely align exactly)."""
    try:
        af, bf = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if af == bf:
        return True
    scale = min(abs(af), abs(bf))
    return abs(af - bf) <= max(STRIKE_TOL, scale * STRIKE_TOL_FRAC)


def _strike_gap_frac(a: float | None, b: float | None) -> float | None:
    """Relative strike distance |a−b| / mean(a,b)."""
    if a is None or b is None:
        return None
    lo, hi = min(a, b), max(a, b)
    mean = (a + b) / 2
    return (hi - lo) / mean if mean else None


# Pairing ceiling for the scanner; each paper strategy filters further via max_strike_gap.
PAIR_MAX_STRIKE_GAP = float(os.environ.get("CRYPTO_ARB_MAX_PAIR_GAP", "0.01"))


def _canonical_expiry(value: str) -> str | None:
    """Normalize venue timestamps to an exact UTC second, never just a minute."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _rule_operators(text: str) -> tuple[str, str] | None:
    """Extract explicit YES/NO threshold operators from official venue text."""
    value = (text or "").lower()
    if re.search(r"greater than or equal|at or above|\bor above\b|≥", value):
        return ">=", "<"
    if re.search(r"(^|\s)>\s*\$?|greater than|strictly above", value):
        return ">", "<="
    return None


def _payout_coverage(yes_quote: dict, no_quote: dict) -> bool:
    """True when YES/NO legs cover every price (allows nearby strikes when ordered)."""
    yes_strike = yes_quote.get("strike")
    no_strike = no_quote.get("strike")
    if yes_strike is None or no_strike is None:
        return False
    yes_op = yes_quote.get("yes_operator")
    no_op = no_quote.get("no_operator")
    if yes_op not in (">", ">=") or no_op not in ("<", "<="):
        return False
    if yes_op == ">" and no_op == "<":
        return False
    # YES threshold must be at or below the NO threshold — otherwise prices between
    # the strikes pay neither leg.
    try:
        yes_dec = price_decimal(normalize_strike(yes_strike))
        no_dec = price_decimal(normalize_strike(no_strike))
        if yes_dec is None or no_dec is None:
            return False
        return yes_dec <= no_dec
    except (InvalidOperation, ValueError):
        return False


# ---- live snapshot + cumulative counter -------------------------------------
_LOCK = threading.Lock()
_SNAPSHOT: dict = {"markets": [], "opportunities": [], "coverage": [], "at": 0.0, "stats": {}}
_SEEN_OPPS: set[str] = set()
_POLY_REF: dict[tuple, float] = {}   # (coin, window_start_epoch) -> spot at first sighting
_DEMAND = {"at": 0.0}
_started = False

poly_chainlink.start()


def touch() -> None:
    _DEMAND["at"] = time.time()


def _http_json(url: str, bucket: rate_limit.TokenBucket | None = None, timeout: int = 15):
    if bucket:
        bucket.acquire()
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "kalshi-poly-search/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---- spot prices (used as Polymarket's reference at window open) -------------
def _fetch_spot() -> dict[str, float]:
    """{COIN: last price} from crypto.com's public exchange tickers (one call)."""
    out: dict[str, float] = {}
    try:
        data = _http_json(CRYPTOCOM_TICKERS)
    except Exception:
        return out
    want = {f"{c}_USD": c for c in COINS} | {f"{c}_USDT": c for c in COINS}
    for item in ((data.get("result") or {}).get("data")) or []:
        coin = want.get(item.get("i"))
        if coin and coin not in out:
            dec = price_decimal(item.get("a"))
            if dec is not None:
                out[coin] = float(dec)
    return out


# ---- venue fetchers: each emits unified quote dicts -------------------------
# quote = {coin, expiry, strike, venue, yes, no, ref}  (yes/no = cost to buy that side)
def _cc_strike(contract_title: str, symbol: str) -> float | None:
    m = _CC_STRIKE_RE.search(contract_title or "")
    if m:
        try:
            return normalize_strike(m.group(1).replace(",", ""))
        except ValueError:
            pass
    parts = (symbol or "").split("_")
    if len(parts) >= 3 and parts[2].isdigit():
        val = int(parts[2])
        sym = parts[0].upper()
        if sym.startswith("BTC"):
            return normalize_strike(val / 100.0)
        elif sym.startswith("ETH"):
            return normalize_strike(val / 100.0)
        elif sym.startswith("SOL"):
            return normalize_strike(val / 100.0)
        elif sym.startswith("DOGE"):
            return normalize_strike(val / 100000.0)
        elif sym.startswith("XRP"):
            return normalize_strike(val / 10000.0)
        elif sym.startswith("ADA"):
            return normalize_strike(val / 10000.0)
        elif sym.startswith("LINK"):
            return normalize_strike(val / 1000.0)
        else:
            return normalize_strike(val / 100.0)
    return None


def _cryptocom_coin(coin: str, cfg: dict) -> list[dict]:
    data = catalog._cryptocom_get(
        f"/v1/events?limit=100&status=active&asset_type=predicts&event_kinds={coin}")
    events = (data or {}).get("data") or []
    timed = [e for e in events
             if e.get("duration") and e.get("event_kind_sub_asset_type") == "crypto"
             and "price" in (e.get("title") or "").lower()]
    if not timed:
        return []
    contracts = catalog._fetch_cryptocom_contracts([e["id"] for e in timed if e.get("id")])
    quotes: list[dict] = []
    for e in timed:
        expiry = _canonical_expiry(e.get("payout_date") or e.get("close_date") or "")
        if not expiry:
            continue
        slug = e.get("id")
        for c in contracts.get(e.get("id"), []):
            contract_title = c.get("contract_title", "")
            strike = _cc_strike(contract_title, c.get("symbol", ""))
            operators = _rule_operators(contract_title)
            if strike is None or c.get("yes") in (None, ""):
                continue
            yes = float(c["yes"])
            no = c.get("no")
            quotes.append({
                "coin": coin, "expiry": expiry, "strike": strike, "venue": "cryptocom",
                "yes": yes, "no": float(no) if no not in (None, "") else round(1 - yes, 2),
                "slug": slug,
                "contract_id": str(c.get("id") or ""),
                "symbol": c.get("symbol") or "",
                "strike_verified": True,
                "strike_source": "contract title" if _CC_STRIKE_RE.search(contract_title) else "contract symbol",
                "strike_evidence": contract_title or c.get("symbol", ""),
                "settlement_rule_verified": operators is not None,
                "yes_operator": operators[0] if operators else None,
                "no_operator": operators[1] if operators else None,
            })
    return quotes


def _fetch_cryptocom() -> list[dict]:
    return _fanout(_cryptocom_coin)


_SUB_TARGET_RE = re.compile(r"Target Price:\s*\$([\d,]+(?:\.\d+)?)", re.I)
_SUB_ABOVE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*or above", re.I)
_TICKER_STRIKE_RE = re.compile(r"-T([\d.]+)$")


def _kalshi_strike(m: dict) -> float | None:
    sub = m.get("yes_subtitle") or m.get("yes_sub_title") or ""
    if "below" in sub.lower() or "less" in sub.lower():
        return None
    for rx in (_SUB_TARGET_RE, _SUB_ABOVE_RE):
        mm = rx.search(sub)
        if mm:
            return normalize_strike(mm.group(1).replace(",", ""))
    mm = _TICKER_STRIKE_RE.search(m.get("ticker") or "")
    return normalize_strike(mm.group(1)) if mm else None


def _kalshi_coin(coin: str, cfg: dict) -> list[dict]:
    """Kalshi short-term threshold/target markets with live quotes (search feed)."""
    horizon = time.time() + _KALSHI_MAX_HORIZON
    try:
        data = _http_json(f"{KALSHI_SEARCH}?query={cfg['kalshi']}&status=open&limit=50",
                          _KALSHI_BUCKET)
    except Exception:
        return []
    quotes: list[dict] = []
    for ev in data.get("current_page") or []:
        for m in ev.get("markets") or []:
            ticker = m.get("ticker") or ""
            sm = _KALSHI_SHORT_SERIES.match(ticker.split("-")[0])
            if not sm or sm.group(1) != coin:
                continue
            close = m.get("close_ts") or m.get("expected_expiration_ts") or ""
            expiry = _canonical_expiry(close)
            if not expiry:
                continue
            try:
                ts = datetime.fromisoformat(expiry + "+00:00").timestamp()
            except (ValueError, AttributeError):
                continue
            if ts > horizon or ts < time.time():
                continue
            strike = _kalshi_strike(m)
            if strike is None:
                continue
            rule_evidence = m.get("yes_subtitle") or m.get("yes_sub_title") or ""
            operators = _rule_operators(rule_evidence)
            ask, bid = m.get("yes_ask"), m.get("yes_bid")
            quotes.append({
                "coin": coin, "expiry": expiry, "strike": strike, "venue": "kalshi",
                "yes": ask / 100.0 if ask else None,      # buy YES at the ask
                "no": (100 - bid) / 100.0 if bid else None,  # buy NO = 1 - YES bid
                "ticker": ticker,
                "strike_verified": True,
                "strike_source": "market subtitle" if (m.get("yes_subtitle") or m.get("yes_sub_title")) else "market ticker",
                "strike_evidence": m.get("yes_subtitle") or m.get("yes_sub_title") or ticker,
                "settlement_rule_verified": operators is not None,
                "yes_operator": operators[0] if operators else None,
                "no_operator": operators[1] if operators else None,
            })
    return quotes


def _fetch_kalshi() -> list[dict]:
    return _fanout(_kalshi_coin)


def _polymarket_coin(coin: str, cfg: dict, spot: dict[str, float]) -> list[dict]:
    """Polymarket 5-min up/down with metadata or recorded RTDS Chainlink T0."""
    quotes: list[dict] = []
    win = int(time.time() // 300 * 300)             # current 5-min window start epoch
    for start in (win, win - 300):                  # current + the one just closing
        slug = f"{cfg['poly']}-updown-5m-{start}"
        try:
            data = _http_json(f"{GAMMA_EVENTS}?slug={slug}", _POLY_BUCKET, timeout=10)
        except Exception:
            continue
        evs = data if isinstance(data, list) else (data or {}).get("events", [])
        if not evs or not evs[0].get("markets"):
            continue
        ev = evs[0]
        m = ev["markets"][0]
        end = _canonical_expiry(m.get("endDate") or m.get("endDateIso") or "")
        if not end:
            continue

        # Extract the official starting target price from Polymarket event metadata
        price_to_beat = None
        metadata = ev.get("eventMetadata") or {}
        if isinstance(metadata, dict):
            ptb = metadata.get("priceToBeat")
            if ptb is not None:
                try:
                    price_to_beat = float(ptb)
                except (ValueError, TypeError):
                    price_to_beat = None

        recorded_ref = None
        if price_to_beat is not None:
            ref = normalize_strike(price_to_beat)
            if ref is None:
                continue
            strike_verified = True
            strike_source = "eventMetadata.priceToBeat"
            strike_evidence = str(metadata.get("priceToBeat"))
            ref_timestamp_ms = start * 1000
            ref_delay_ms = 0
        else:
            # Crypto PTB is reconstructed from Polymarket's own RTDS Chainlink
            # stream: the earliest tick at/after the exact five-minute boundary.
            # Never substitute a later crypto.com spot observation.
            recorded_ref = poly_chainlink.reference(coin, start)
            if not recorded_ref:
                continue
            ref = normalize_strike(recorded_ref["price"])
            if ref is None:
                continue
            strike_verified = recorded_ref.get("verified") is True
            strike_source = "Polymarket RTDS Chainlink T0"
            strike_evidence = (
                f"{coin.lower()}/usd @ {recorded_ref.get('tick_timestamp_ms')} "
                f"(T0+{recorded_ref.get('delay_ms')}ms)"
            )
            ref_timestamp_ms = recorded_ref.get("tick_timestamp_ms")
            ref_delay_ms = recorded_ref.get("delay_ms")
        _POLY_REF[(coin, start)] = ref
        rule_evidence = " ".join(str(value or "") for value in (
            m.get("question"), m.get("description"), ev.get("description")
        ))
        operators = _rule_operators(rule_evidence)

        bid, ask = m.get("bestBid"), m.get("bestAsk")
        try:
            up_ask = float(ask) if ask is not None else None                 # buy Up (YES)
            down_ask = round(1 - float(bid), 4) if bid is not None else None  # buy Down (NO)
        except (TypeError, ValueError):
            up_ask = down_ask = None
        quotes.append({
            "coin": coin, "expiry": end, "strike": ref, "venue": "polymarket",
            "yes": up_ask, "no": down_ask, "ref": ref, "slug": slug,
            "strike_verified": strike_verified,
            "strike_source": strike_source,
            "strike_evidence": strike_evidence,
            "strike_timestamp_ms": ref_timestamp_ms,
            "strike_delay_ms": ref_delay_ms,
            "strike_observation": recorded_ref,
            "settlement_rule_verified": operators is not None,
            "yes_operator": operators[0] if operators else None,
            "no_operator": operators[1] if operators else None,
            "rule_evidence": rule_evidence,
        })
    return quotes


def _fetch_polymarket(spot: dict[str, float]) -> list[dict]:
    return _fanout(lambda coin, cfg: _polymarket_coin(coin, cfg, spot))


def _quote_per_venue(q: dict) -> dict:
    pv = {
        "yes": q.get("yes"),
        "no": q.get("no"),
        "strike": q["strike"] if q.get("strike") is None else normalize_strike(q["strike"]),
        "strike_verified": q.get("strike_verified") is True,
        "strike_source": q.get("strike_source"),
        "strike_evidence": q.get("strike_evidence"),
        "strike_timestamp_ms": q.get("strike_timestamp_ms"),
        "strike_delay_ms": q.get("strike_delay_ms"),
        "settlement_rule_verified": q.get("settlement_rule_verified") is True,
        "yes_operator": q.get("yes_operator"),
        "no_operator": q.get("no_operator"),
        "rule_evidence": q.get("rule_evidence"),
    }
    if q.get("slug"):
        pv["slug"] = q["slug"]
    if q.get("ticker"):
        pv["ticker"] = q["ticker"]
    if q.get("contract_id"):
        pv["contract_id"] = q["contract_id"]
    if q.get("symbol"):
        pv["symbol"] = q["symbol"]
    return pv


# ---- general N-venue matcher + arbitrage formula ----------------------------
def compute(quotes: list[dict]) -> dict:
    """Match verified quotes by coin + expiry, pairing cross-venue YES/NO legs when
    payout coverage holds and strike gap is within PAIR_MAX_STRIKE_GAP."""
    groups: dict[tuple, list] = {}
    for q in quotes:
        if (
            q.get("expiry")
            and q.get("strike_verified") is True
            and q.get("settlement_rule_verified") is True
        ):
            groups.setdefault((q["coin"], q["expiry"]), []).append(q)

    markets: list[dict] = []
    opportunities: list[dict] = []
    for (coin, expiry), qs in groups.items():
        yes_qs = [q for q in qs if q.get("yes") is not None]
        no_qs = [q for q in qs if q.get("no") is not None]
        if len({q["venue"] for q in qs}) < 2:
            continue
        expires_in_s = None
        try:
            exp_ts = datetime.fromisoformat(expiry + "+00:00").timestamp()
            expires_in_s = max(0, int(exp_ts - time.time()))
        except (ValueError, TypeError):
            pass

        for yes_q in yes_qs:
            for no_q in no_qs:
                if yes_q["venue"] == no_q["venue"]:
                    continue
                if not _payout_coverage(yes_q, no_q):
                    continue
                gap = _strike_gap_frac(yes_q["strike"], no_q["strike"])
                if gap is None or gap > PAIR_MAX_STRIKE_GAP:
                    continue
                edge = round(1 - (yes_q["yes"] + no_q["no"]), 4)
                if edge <= 0:
                    continue
                per_venue = {
                    yes_q["venue"]: _quote_per_venue(yes_q),
                    no_q["venue"]: _quote_per_venue(no_q),
                }
                row = {
                    "coin": coin,
                    "expiry": expiry,
                    "strike": yes_q["strike"],
                    "yes_strike": yes_q["strike"],
                    "no_strike": no_q["strike"],
                    "strike_gap": round(gap, 8),
                    "venues": sorted(per_venue.keys()),
                    "per_venue": per_venue,
                    "exact_strike_match": _strike_match(yes_q["strike"], no_q["strike"]),
                    "strike_verified": True,
                    "settlement_rules_verified": True,
                    "yes_venue": yes_q["venue"],
                    "yes_cost": yes_q["yes"],
                    "no_venue": no_q["venue"],
                    "no_cost": no_q["no"],
                    "max_arb": edge,
                    "priced": True,
                    "expires_in_s": expires_in_s,
                }
                key = (
                    f"{coin}|{expiry}|{yes_q['strike']:.6f}|{yes_q['venue']}"
                    f">{no_q['strike']:.6f}|{no_q['venue']}"
                )
                markets.append(row)
                with _LOCK:
                    _SEEN_OPPS.add(key)
                opportunities.append(row)

    opportunities.sort(key=lambda r: -r["max_arb"])
    markets.sort(key=lambda r: (-r["max_arb"], r["coin"], r["expiry"], r["strike"]))
    return {"markets": markets, "opportunities": opportunities}


# ---- coverage (per-coin expiry alignment across venues) ---------------------
def _minute_label(minute: str) -> str:
    try:
        return datetime.fromisoformat(minute + "+00:00").astimezone(
            _ET).strftime("%-m/%-d %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except ValueError:
        return minute


def _strike_gap_pct(strikes: list[float]) -> float | None:
    if len(strikes) < 2:
        return None
    lo, hi = min(strikes), max(strikes)
    mean = sum(strikes) / len(strikes)
    return round((hi - lo) / mean * 100, 3) if mean else None


def _coverage(quotes: list[dict]) -> list[dict]:
    by: dict[str, dict[str, set]] = {}
    strikes_by_coin_exp: dict[tuple, list[float]] = {}
    for q in quotes:
        if q.get("expiry"):
            by.setdefault(q["coin"], {}).setdefault(q["venue"], set()).add(q["expiry"])
            if q.get("strike") is not None:
                strikes_by_coin_exp.setdefault((q["coin"], q["expiry"]), []).append(q["strike"])
    rows = []
    for coin in sorted(by):
        vmap = by[coin]
        all_exp = set().union(*vmap.values()) if vmap else set()
        # an expiry that ≥2 venues share is a match candidate
        shared = sorted(e for e in all_exp if sum(e in vmap.get(v, set()) for v in VENUES) >= 2)
        shared_rows = []
        gap_vals: list[float] = []
        for e in shared:
            gap = _strike_gap_pct(strikes_by_coin_exp.get((coin, e), []))
            if gap is not None:
                gap_vals.append(gap)
            shared_rows.append({"m": e, "label": _minute_label(e), "gap_pct": gap})
        rows.append({
            "coin": coin,
            "venues": {v: [{"m": e, "label": _minute_label(e)} for e in sorted(vmap.get(v, set()))]
                       for v in VENUES if vmap.get(v)},
            "shared": shared_rows,
            "nearest_gap_pct": min(gap_vals) if gap_vals else None,
        })
    return rows


def _refresh_once() -> dict:
    spot = _fetch_spot()
    quotes = _fetch_cryptocom() + _fetch_kalshi() + _fetch_polymarket(spot)
    result = compute(quotes)
    now = time.time()
    with _LOCK:
        cumulative = len(_SEEN_OPPS)
    snap = {
        "markets": result["markets"],
        "opportunities": result["opportunities"],
        "coverage": _coverage(quotes),
        "formula": ARB_FORMULA,
        "at": now,
        "stats": {
            "matched_markets": len(result["markets"]),
            "priced": sum(1 for m in result["markets"] if m["priced"]),
            "live_opportunities": len(result["opportunities"]),
            "cumulative_opportunities": cumulative,
            "coins": sorted({q["coin"] for q in quotes}),
            "venues_live": sorted({q["venue"] for q in quotes}),
            "polymarket_chainlink": poly_chainlink.status(),
            "updated": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "poll_seconds": POLL_SECONDS,
        },
    }
    with _LOCK:
        _SNAPSHOT.update(snap)
    return snap


def snapshot() -> dict:
    touch()
    with _LOCK:
        return dict(_SNAPSHOT)


def start_poller() -> None:
    global _started
    if _started or os.environ.get("CRYPTO_ARB_ENABLE", "1") != "1":
        return
    _started = True

    def _run():
        while True:
            if time.time() - _DEMAND["at"] > 30:      # idle unless watched
                time.sleep(2)
                continue
            try:
                _refresh_once()
            except Exception as e:
                print("crypto_arb refresh error:", e)
            time.sleep(POLL_SECONDS)

    threading.Thread(target=_run, daemon=True, name="crypto-arb").start()
