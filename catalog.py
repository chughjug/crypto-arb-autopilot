"""Bulk market catalog fetch with SQLite persistence (low RAM footprint)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import catalog_db
import exact_match
import rate_limit

KALSHI_EVENTS = "https://api.elections.kalshi.com/v1/search/series"
POLY_EVENTS = "https://gamma-api.polymarket.com/events"
# Crypto.com "Predict" prediction markets (YES/NO contracts like Kalshi/Polymarket).
# Reached through the web app's public proxy. asset_type=sports = game matchups /
# league futures; asset_type=predicts = the Events tab (crypto price, politics).
CRYPTOCOM_PREDICT_BASE = "https://web.crypto.com/api/proxy/public/knock-out/predictions/public/api"
CRYPTOCOM_EVENT_URL = "https://web.crypto.com/hub/predict/events/details/{id}"
CRYPTOCOM_ASSET_TYPES = [s.strip() for s in os.environ.get(
    "CRYPTOCOM_ASSET_TYPES", "sports,predicts").split(",") if s.strip()]
CRYPTOCOM_PAGE = int(os.environ.get("CRYPTOCOM_PAGE_SIZE", "100"))
CRYPTOCOM_MAX_PAGES = int(os.environ.get("CRYPTOCOM_MAX_PAGES", "40"))
CRYPTOCOM_CONTRACT_BATCH = int(os.environ.get("CRYPTOCOM_CONTRACT_BATCH", "25"))
CATALOG_TTL = int(os.environ.get("CATALOG_TTL", "300"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
KALSHI_PAGE = int(os.environ.get("KALSHI_PAGE_SIZE", "200"))
POLY_PAGE = int(os.environ.get("POLY_PAGE_SIZE", "100"))
POLY_MAX_PAGES = int(os.environ.get("POLY_MAX_PAGES", "25"))  # ~2500 events (API offset cap)
POLY_MAX_OFFSET = int(os.environ.get("POLY_MAX_OFFSET", "2000"))
POLY_MIN_VOLUME = float(os.environ.get("POLY_MIN_VOLUME", "0"))
IS_HEROKU = bool(os.environ.get("DYNO"))
POLY_SUPPLEMENT_SLUGS = [
    s.strip() for s in os.environ.get(
        "POLY_SUPPLEMENT_SLUGS",
        "2027-french-presidential-election-who-will-be-on-the-ballot,"
        "next-french-presidential-election,"
        "2027-french-presidential-election-national-rally-candidate",
    ).split(",")
    if s.strip()
]
POLY_VOLUME_PAGES = int(os.environ.get("POLY_VOLUME_PAGES", "5"))
BATCH_SIZE = int(os.environ.get("CATALOG_BATCH_SIZE", "200"))

_warming = False
_ready = threading.Event()
_lock = threading.Lock()


def _get(url: str) -> object:
    req = Request(url, headers={"Accept": "application/json",
                                "User-Agent": "kalshi-poly-search/1.0"})
    for attempt in range(4):
        # Share the process-wide per-host budget so catalog warming doesn't race the
        # server's per-user requests into a 429.
        rate_limit.acquire(url)
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(rate_limit.note_429(url, e.headers.get("Retry-After")))
                continue
            raise
        except (URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


def _poly_yes_cents(m: dict):
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        for name, p in zip(outcomes, prices):
            if str(name).strip().lower() == "yes":
                return round(float(p) * 100)
        if prices:
            return round(float(prices[0]) * 100)
    except (ValueError, TypeError):
        pass
    return None


def _dollars_cents(v) -> int | None:
    if v is None:
        return None
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return None


def _kalshi_market_prices(m: dict) -> tuple[int | None, int | None, int | None]:
    """Kalshi v2 uses yes_*_dollars strings; older payloads used integer cents."""
    bid = m.get("yes_bid")
    ask = m.get("yes_ask")
    last = m.get("last_price")
    if bid is None:
        bid = _dollars_cents(m.get("yes_bid_dollars"))
    if ask is None:
        ask = _dollars_cents(m.get("yes_ask_dollars"))
    if last is None:
        last = _dollars_cents(m.get("last_price_dollars"))
    return bid, ask, last


def normalize_kalshi_event(ev: dict) -> dict | None:
    ticker = ev.get("event_ticker", "")
    if not ticker:
        return None
    series = (ticker.split("-")[0] or "").lower()
    markets = []
    for m in ev.get("markets", []):
        if m.get("status") == "closed":
            continue
        bid, ask, last = _kalshi_market_prices(m)
        # The events endpoint returns the per-outcome name in yes_sub_title /
        # subtitle (NOT yes_subtitle). For candidate markets `title` is just the
        # repeated question, so reading the wrong field made every outcome
        # identical and broke contract matching (elections, awards, nominees).
        name = (m.get("yes_sub_title") or m.get("subtitle")
                or m.get("yes_subtitle") or m.get("title") or m.get("ticker", ""))
        markets.append({
            "label": exact_match.contract_subject(name) or name,
            "full_label": name,
            "ticker": m.get("ticker"),
            "bid": bid,
            "ask": ask,
            "last": last,
            "image": m.get("image_url_light_mode") or m.get("image_url_dark_mode") or m.get("image_url") or m.get("image") or m.get("icon") or "",
            "end_date": m.get("close_date") or m.get("expiration_date") or m.get("close_ts") or m.get("expected_expiration_date") or m.get("expected_expiration_ts"),
        })
    if not markets:
        return None
    volume = int(ev.get("total_volume") or 0) // 100

    pm = ev.get("product_metadata") or {}
    image = pm.get("custom_image_url") or pm.get("image_url_light_mode") or ev.get("image_url") or ev.get("image") or ""
    if not image:
        for m in markets:
            if m.get("image"):
                image = m["image"]
                break

    close_dates = [m.get("end_date") for m in markets if m.get("end_date")]
    event_end_date = ev.get("target_datetime") or (close_dates[0] if close_dates else None)

    return {
        "source": "kalshi",
        "title": ev.get("event_title") or ev.get("title") or ev.get("series_title") or "",
        "subtitle": ev.get("sub_title") or ev.get("event_subtitle") or "",
        "category": ev.get("category") or "",
        "volume": volume,
        "ticker": ticker,
        "url": f"https://kalshi.com/markets/{series}" if series else "https://kalshi.com",
        "image": image,
        "markets": markets,
        "end_date": event_end_date,
    }


def normalize_poly_event(ev: dict) -> dict | None:
    if ev.get("closed") or not ev.get("active", True):
        return None
    slug = ev.get("slug", "")
    markets = []
    for m in ev.get("markets", []):
        if m.get("closed"):
            continue
        yes = _poly_yes_cents(m)
        bid = m.get("bestBid")
        ask = m.get("bestAsk")
        bid_c = round(bid * 100) if isinstance(bid, (int, float)) else None
        ask_c = round(ask * 100) if isinstance(ask, (int, float)) else None
        if bid_c is None and yes is not None:
            bid_c = yes
        if ask_c is None and yes is not None:
            ask_c = yes
        markets.append({
            "label": m.get("groupItemTitle") or m.get("question") or "",
            "id": m.get("id"),
            "bid": bid_c,
            "ask": ask_c,
            "last": yes,
            "image": m.get("icon") or m.get("image") or "",
            "end_date": m.get("endDate") or m.get("endDateIso"),
        })
    if not markets:
        return None
    image = ev.get("image") or ev.get("icon") or ""
    if not image:
        for m in markets:
            if m.get("image"):
                image = m["image"]
                break

    close_dates = [m.get("end_date") for m in markets if m.get("end_date")]
    event_end_date = ev.get("endDate") or ev.get("endDateIso") or (close_dates[0] if close_dates else None)

    return {
        "source": "polymarket",
        "id": str(ev.get("id", "")),
        "title": ev.get("title") or "",
        "subtitle": "",
        "category": (ev.get("tags") or [{}])[0].get("label", "") if ev.get("tags") else "",
        "volume": int(float(ev.get("volume") or 0)),
        "url": f"https://polymarket.com/event/{slug}",
        "slug": slug,
        "image": image,
        "markets": markets,
        "end_date": event_end_date,
    }


import subprocess as _subprocess
import shutil as _shutil

_CRYPTOCOM_CURL = _shutil.which("curl") or "/usr/bin/curl"
_CRYPTOCOM_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Map crypto.com's "predicts" event_kind codes to our explore categories.
_CRYPTOCOM_KIND_CATEGORY = {
    "CRYPT": "Crypto", "ELECT": "Politics", "EC": "Economics",
    "COMPANIES": "Financials", "CUL": "Culture", "CLIM": "Weather",
}


def _cryptocom_get(path: str) -> dict | None:
    """GET the Predict API via the system curl binary.

    The endpoint sits behind Akamai bot protection that 403s Python's urllib TLS
    fingerprint, so we shell out to curl (HTTP/2), which passes. Returns the parsed
    JSON `data` envelope, or None on failure — a crypto.com outage is non-fatal.
    """
    url = f"{CRYPTOCOM_PREDICT_BASE}{path}"
    for attempt in range(3):
        rate_limit.acquire(url)
        try:
            proc = _subprocess.run(
                [_CRYPTOCOM_CURL, "-s", "--http2", "--max-time", str(HTTP_TIMEOUT),
                 "-H", f"User-Agent: {_CRYPTOCOM_UA}",
                 "-H", "Accept: application/json",
                 "-H", "Referer: https://web.crypto.com/explore/predict/events",
                 url],
                capture_output=True, timeout=HTTP_TIMEOUT + 5,
            )
            data = json.loads(proc.stdout)
            if data.get("code") == 0:
                return data.get("data") or {}
        except (json.JSONDecodeError, _subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(2 ** attempt)
    return None


def _fetch_cryptocom_events(asset_type: str) -> list[dict]:
    """Paginate all active events for one asset_type (sports | predicts)."""
    events: list[dict] = []
    after: str | None = None
    for _ in range(CRYPTOCOM_MAX_PAGES):
        q = f"?limit={CRYPTOCOM_PAGE}&status=active&asset_type={asset_type}"
        if after:
            q += f"&starting_after={after}"
        page = _cryptocom_get(f"/v1/events{q}")
        rows = (page or {}).get("data") or []
        if not rows:
            break
        events.extend(rows)
        after = rows[-1].get("id")
        if not (page or {}).get("has_more") or not after:
            break
    return events


def _fetch_cryptocom_contracts(event_ids: list[str]) -> dict[str, list[dict]]:
    """Batch-fetch contracts (prices) for many events. Returns {event_id: contracts}."""
    out: dict[str, list[dict]] = {}
    for i in range(0, len(event_ids), CRYPTOCOM_CONTRACT_BATCH):
        chunk = event_ids[i:i + CRYPTOCOM_CONTRACT_BATCH]
        data = _cryptocom_get(f"/v2/contracts?event_id={','.join(chunk)}")
        for ev in (data or {}).get("data") or []:
            eid = ev.get("event_id")
            if eid:
                out[eid] = ev.get("contracts") or []
    return out


def _cryptocom_price_cents(v) -> int | None:
    """A contract's yes/no price is a fraction of $1 (0.49 → 49¢)."""
    try:
        c = round(float(v) * 100)
    except (TypeError, ValueError):
        return None
    return c if 0 <= c <= 100 else None


def normalize_cryptocom_event(ev: dict, contracts: list[dict]) -> dict | None:
    """Turn a Predict event + its contracts into a catalog event with YES/NO markets."""
    eid = ev.get("id")
    if not eid:
        return None
    sub = (ev.get("event_kind_sub_asset_type") or "").lower()
    asset = (ev.get("event_kind_asset_type") or "").lower()
    if sub == "crypto":
        category = "Crypto"
    elif asset == "sports":
        category = "Sports"
    else:
        category = _CRYPTOCOM_KIND_CATEGORY.get(ev.get("event_kind", ""), "Politics")

    markets = []
    for c in contracts:
        if c.get("status") not in (None, "active", "open"):
            continue
        label = (c.get("contract_title") or c.get("participant_name")
                 or c.get("team_name") or "")
        yes = _cryptocom_price_cents(c.get("yes"))
        no = _cryptocom_price_cents(c.get("no"))
        team = c.get("team") or {}
        markets.append({
            "label": label,
            "id": c.get("id"),
            "bid": yes,
            "ask": yes,
            "last": yes,
            "no": no,
            "image": team.get("logo_url") or team.get("icon_url") or "",
            "end_date": ev.get("close_date") or ev.get("event_date"),
        })
    if not markets:
        return None

    image = (ev.get("web_landing_img") or ev.get("landing_img")
             or ev.get("web_banner") or ev.get("details_img") or "")
    slug = ev.get("slug") or eid
    out = {
        "source": "cryptocom",
        "id": eid,
        "title": ev.get("title") or "",
        "subtitle": ev.get("subtitle") or "",
        "category": category,
        "volume": 0,   # the public Predict API exposes no volume/open-interest
        "ticker": slug,
        "url": CRYPTOCOM_EVENT_URL.format(id=eid),
        "image": image,
        "markets": markets,
        "end_date": ev.get("close_date") or ev.get("event_date"),
    }
    # Tag short-term timed crypto markets ("Bitcoin price Today at 1:00 pm ET") with
    # a parsed window so the /crypto page can slot them beside Kalshi/Poly cadences.
    short = _cryptocom_short_window(ev)
    if short:
        out["cc_short"] = short
    return out


_CRYPTOCOM_FREQ = {300: "5m", 600: "10m", 900: "15m", 1200: "20m",
                   1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h"}


def _cryptocom_short_window(ev: dict) -> dict | None:
    """For a timed crypto price event, return {coin,start,end,freq,dur} (epoch secs)."""
    if ev.get("event_kind_sub_asset_type") != "crypto":
        return None
    dur = ev.get("duration")
    expiry = ev.get("payout_date") or ev.get("close_date")
    if not dur or not expiry or "price" not in (ev.get("title") or "").lower():
        return None
    try:
        end = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None
    dur = int(dur)
    if dur > 7200:   # only short-term cadences (≤2h); daily/weekly stay regular events
        return None
    freq = _CRYPTOCOM_FREQ.get(dur) or (f"{dur // 3600}h" if dur >= 3600 else f"{dur // 60}m")
    return {"coin": ev.get("event_kind", ""), "start": end - dur,
            "end": end, "freq": freq, "dur": dur}


def _stream_cryptocom_to_db() -> set[str]:
    """Fetch all crypto.com Predict events (sports + events tab) plus their contracts,
    normalize to YES/NO catalog events, and upsert."""
    keep: set[str] = set()
    for asset_type in CRYPTOCOM_ASSET_TYPES:
        events = _fetch_cryptocom_events(asset_type)
        if not events:
            continue
        contracts = _fetch_cryptocom_contracts([e["id"] for e in events if e.get("id")])
        batch: list[dict] = []
        for ev in events:
            norm = normalize_cryptocom_event(ev, contracts.get(ev.get("id"), []))
            if not norm:
                continue
            batch.append(norm)
            if len(batch) >= BATCH_SIZE:
                keep.update(catalog_db.upsert_batch("cryptocom", batch))
                batch.clear()
        if batch:
            keep.update(catalog_db.upsert_batch("cryptocom", batch))
    return keep


def _fetch_kalshi_page(cursor: str | None) -> dict:
    q = {"limit": KALSHI_PAGE, "status": "open"}
    if cursor:
        q["cursor"] = cursor
    return _get(f"{KALSHI_EVENTS}?{urlencode(q)}") or {}


def _stream_kalshi_to_db() -> set[str]:
    """Fetch Kalshi pages and upsert incrementally — never hold full list in RAM.

    Upserts in place (no up-front clear) and returns the set of stored event ids
    so the caller can prune stale rows AFTER streaming. This keeps the table fully
    populated throughout the refresh — readers never see an empty venue.
    """
    keep: set[str] = set()
    batch: list[dict] = []
    data = _fetch_kalshi_page(None)
    while True:
        page = data.get("current_page") or []
        for ev in page:
            norm = normalize_kalshi_event(ev)
            if not norm:
                continue
            batch.append(norm)
            if len(batch) >= BATCH_SIZE:
                keep.update(catalog_db.upsert_batch("kalshi", batch))
                batch.clear()
        cursor = data.get("next_cursor")
        if not cursor or not page:
            break
        data = _fetch_kalshi_page(cursor)
    if batch:
        keep.update(catalog_db.upsert_batch("kalshi", batch))
    return keep


def _fetch_poly_page(offset: int, ascending: bool = True) -> list[dict]:
    q = {"limit": POLY_PAGE, "closed": "false", "active": "true",
         "order": "id", "ascending": "true" if ascending else "false",
         "offset": offset}
    if POLY_MIN_VOLUME > 0:
        q["volume_num_min"] = str(int(POLY_MIN_VOLUME))
    data = _get(f"{POLY_EVENTS}?{urlencode(q)}")
    return data if isinstance(data, list) else (data or {}).get("events", [])


def _stream_poly_to_db() -> set[str]:
    """Fetch Polymarket pages and upsert incrementally (no up-front clear).

    Returns the set of stored event ids for post-stream pruning.
    """
    keep: set[str] = set()
    seen: set[str] = set()
    batch: list[dict] = []
    for ascending in (True, False):
        offset = 0
        while offset <= POLY_MAX_OFFSET:
            try:
                page = _fetch_poly_page(offset, ascending=ascending)
            except HTTPError as e:
                if e.code == 422:
                    break
                raise
            if not page:
                break
            for ev in page:
                eid = str(ev.get("id") or "")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                norm = normalize_poly_event(ev)
                if not norm:
                    continue
                batch.append(norm)
                if len(batch) >= BATCH_SIZE:
                    keep.update(catalog_db.upsert_batch("polymarket", batch))
                    batch.clear()
            if len(page) < POLY_PAGE:
                break
            offset += POLY_PAGE
    if batch:
        keep.update(catalog_db.upsert_batch("polymarket", batch))
    return keep


def _fetch_poly_by_slug(slug: str) -> dict | None:
    data = _get(f"{POLY_EVENTS}?{urlencode({'slug': slug})}")
    if isinstance(data, list) and data:
        return data[0]
    return None


def _fetch_poly_volume_page(offset: int) -> list[dict]:
    q = {"limit": POLY_PAGE, "closed": "false", "active": "true",
         "order": "volume", "ascending": "false", "offset": offset}
    data = _get(f"{POLY_EVENTS}?{urlencode(q)}")
    return data if isinstance(data, list) else (data or {}).get("events", [])


def _supplement_poly_to_db(seen: set[str]) -> set[str]:
    """Upsert high-volume and slug-targeted Polymarket events missing from
    id-pagination. Returns the set of stored event ids it added."""
    keep: set[str] = set()
    batch: list[dict] = []

    def add_raw(ev: dict | None) -> None:
        if not ev:
            return
        eid = str(ev.get("id") or "")
        if not eid or eid in seen:
            return
        seen.add(eid)
        norm = normalize_poly_event(ev)
        if not norm:
            return
        batch.append(norm)
        if len(batch) >= BATCH_SIZE:
            keep.update(catalog_db.upsert_batch("polymarket", batch))
            batch.clear()

    for page in range(POLY_VOLUME_PAGES):
        try:
            for ev in _fetch_poly_volume_page(page * POLY_PAGE):
                add_raw(ev)
        except HTTPError:
            break

    for slug in POLY_SUPPLEMENT_SLUGS:
        try:
            add_raw(_fetch_poly_by_slug(slug))
        except (HTTPError, URLError):
            continue

    if batch:
        keep.update(catalog_db.upsert_batch("polymarket", batch))
    return keep


def fetch_all() -> dict:
    """Fetch APIs and persist to SQLite atomically per source: upsert fresh rows,
    then prune stale ones — so readers never see an empty/partial venue. Returns
    stats only (no in-memory lists)."""
    t0 = time.time()
    k_keep = _stream_kalshi_to_db()
    catalog_db.prune_source("kalshi", k_keep)

    p_keep = _stream_poly_to_db()
    # supplement dedupes against what the id-pagination already stored
    p_keep |= _supplement_poly_to_db(set(p_keep))
    catalog_db.prune_source("polymarket", p_keep)

    try:
        c_keep = _stream_cryptocom_to_db()
        catalog_db.prune_source("cryptocom", c_keep)
    except Exception as e:
        # A crypto.com outage must not fail the whole catalog refresh.
        print("cryptocom fetch error:", e)

    elapsed = round(time.time() - t0, 2)
    stats = {
        "kalshi_events": catalog_db.count("kalshi"),
        "poly_events": catalog_db.count("polymarket"),
        "cryptocom_events": catalog_db.count("cryptocom"),
        "fetch_seconds": elapsed,
        "poly_min_volume": POLY_MIN_VOLUME,
        "poly_max_offset": POLY_MAX_OFFSET,
        "cached_at": time.time(),
        "storage": "sqlite",
        "db_path": catalog_db.stats().get("db_path"),
    }
    catalog_db.set_meta(stats)
    return stats


def refresh(force: bool = False) -> dict:
    global _warming
    with _lock:
        if _warming and not force:
            return catalog_db.get_meta()
        _warming = True
    try:
        stats = fetch_all()
        _ready.set()
        return stats
    finally:
        with _lock:
            _warming = False


def ensure_ready(force: bool = False, wait_seconds: float = 0) -> dict:
    """Refresh stale catalog into SQLite if needed; returns stats only (no RAM load)."""
    meta = catalog_db.get_meta()
    age = time.time() - float(meta.get("cached_at") or 0) if meta.get("cached_at") else 9999
    stale = age >= CATALOG_TTL or not meta.get("cached_at")
    has_data = catalog_db.count() > 0

    with _lock:
        warming = _warming

    if force or stale:
        if warming and has_data and not force:
            pass
        elif wait_seconds > 0 and not has_data:
            _ready.wait(timeout=wait_seconds)
        elif not warming or force:
            if force or not has_data:
                refresh(force=True)

    return catalog_db.stats()


def get(
    force: bool = False,
    wait_seconds: float = 0,
    load_events: bool | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Load catalogs from SQLite (refreshes from API if stale).

    On Heroku, load_events defaults to False to avoid OOM; use ensure_ready()
    and match_catalog_from_db() for scans instead of loading full catalogs.
    """
    if load_events is None:
        load_events = not IS_HEROKU
    stats = ensure_ready(force=force, wait_seconds=wait_seconds)
    if not load_events:
        return [], [], stats
    kalshi = catalog_db.load_all("kalshi")
    poly = catalog_db.load_all("polymarket")
    return kalshi, poly, stats


def search_db(query: str, limit: int = 30) -> tuple[list[dict], list[dict]]:
    """Search both platforms from SQLite without loading full catalogs."""
    return (
        catalog_db.search("kalshi", query, limit),
        catalog_db.search("polymarket", query, limit),
    )


def cache_stats() -> dict:
    with _lock:
        warming = _warming
    st = catalog_db.stats()
    st["ttl_seconds"] = CATALOG_TTL
    st["warming"] = warming
    st["ready"] = _ready.is_set() or catalog_db.count() > 0
    return st


def start_background_refresh() -> None:
    """Warm catalog in background. Safe on Heroku — writes to SQLite, not RAM."""
    default = "1"  # DB-backed warm is memory-safe
    if os.environ.get("CATALOG_WARM", default) != "1":
        _ready.set()
        return

    def _run():
        retry_sleep = 30
        while True:
            try:
                age = time.time() - float(catalog_db.get_meta().get("cached_at") or 0)
                if catalog_db.count() == 0 or age >= CATALOG_TTL:
                    refresh(force=True)
                    retry_sleep = 30
                else:
                    _ready.set()
                    retry_sleep = max(10, CATALOG_TTL - age)
            except Exception as e:
                print("catalog warm error:", e)
                _ready.set()
                retry_sleep = 300 if isinstance(e, HTTPError) and getattr(e, "code", None) == 429 else min(retry_sleep * 2, 1800)
            time.sleep(retry_sleep)

    threading.Thread(target=_run, daemon=True, name="catalog-warm").start()
