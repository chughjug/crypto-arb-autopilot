"""Live order book, trades, and whale activity for market detail pages."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

KALSHI_MKT = "https://api.elections.kalshi.com/trade-api/v2/markets/"
POLY_MKT = "https://gamma-api.polymarket.com/markets/"
POLY_DATA_TRADES = "https://data-api.polymarket.com/trades"
POLY_CLOB_BOOK = "https://clob.polymarket.com/book"
KALSHI_CANDLES = "https://api.elections.kalshi.com/trade-api/v2/markets/candlesticks"

_POLY_META_CACHE: dict[str, tuple[float, dict]] = {}
_POLY_META_TTL = 300


def _trade_pages_for_hours(hours: int) -> int:
    if hours <= 1:
        return 1
    if hours <= 24:
        return 2
    if hours <= 72:
        return 3
    return 4


_HTTP_CACHE: dict[str, tuple[float, object]] = {}

def _http_get(url: str, retries: int = 3, ttl: float = 8.0) -> object:
    now = time.time()
    if url in _HTTP_CACHE:
        ts, data = _HTTP_CACHE[url]
        if now - ts < ttl:
            return data

    req = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-poly-search/1.0"})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                _HTTP_CACHE[url] = (time.time(), data)
                return data
        except HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


def outcome_slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s or "outcome"


def _fp(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _kalshi_trade_usd(t: dict) -> float:
    cnt = _fp(t.get("count_fp") or t.get("count"))
    side = (t.get("taker_side") or "").lower()
    if side == "yes":
        return cnt * _fp(t.get("yes_price_dollars"))
    return cnt * _fp(t.get("no_price_dollars"))


def _parse_ts(ts) -> float:
    if ts is None or ts == "":
        return 0.0
    try:
        n = float(ts)
        # Polymarket data-api returns unix seconds (or ms for very large values).
        if n > 1e12:
            return n / 1000.0
        if n > 1e9:
            return n
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def kalshi_trades_since(ticker: str, min_ts: int, max_pages: int = 6) -> list[dict]:
    trades: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"ticker": ticker, "limit": 1000, "min_ts": min_ts}
        if cursor:
            params["cursor"] = cursor
        data = _http_get(f"{KALSHI_MKT}trades?{urlencode(params)}") or {}
        batch = data.get("trades") or []
        trades.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return trades


def _format_trades(raw: list[dict], min_usd: float, whales_only: bool) -> list[dict]:
    out = []
    for t in raw:
        usd = _kalshi_trade_usd(t)
        if whales_only and usd < min_usd:
            continue
        side = (t.get("taker_side") or "yes").lower()
        price_c = round(_fp(t.get("yes_price_dollars" if side == "yes" else "no_price_dollars")) * 100, 1)
        out.append({
            "usd": round(usd, 2),
            "side": side,
            "price_cents": price_c,
            "ts": _parse_ts(t.get("created_time") or ""),
            "time": t.get("created_time") or "",
        })
    out.sort(key=lambda x: -x["ts"])
    return out


def _whale_stats(trades: list[dict], min_usd: float) -> dict:
    yes_usd = no_usd = 0.0
    yes_n = no_n = 0
    whale_trades = []
    for t in trades:
        usd = _kalshi_trade_usd(t)
        if usd < min_usd:
            continue
        side = (t.get("taker_side") or "yes").lower()
        if side == "yes":
            yes_usd += usd
            yes_n += 1
        else:
            no_usd += usd
            no_n += 1
        whale_trades.append(t)
    total = yes_usd + no_usd
    n = yes_n + no_n
    yes_pct = round(100 * yes_usd / total) if total else 0
    no_pct = 100 - yes_pct if total else 0
    yes_trade_pct = round(100 * yes_n / n) if n else 0
    no_trade_pct = 100 - yes_trade_pct if n else 0
    if yes_pct >= 60:
        sentiment = "bullish"
    elif no_pct >= 60:
        sentiment = "bearish"
    else:
        sentiment = "mixed"
    return {
        "total_usd": round(total, 2),
        "trade_count": n,
        "yes_usd": round(yes_usd, 2),
        "no_usd": round(no_usd, 2),
        "yes_pct": yes_pct,
        "no_pct": no_pct,
        "yes_trade_pct": yes_trade_pct,
        "no_trade_pct": no_trade_pct,
        "sentiment": sentiment,
        "raw_whales": whale_trades,
    }


def kalshi_orderbook_levels(ticker: str, limit: int = 24) -> dict:
    """YES-side liquidity ladder (price / size / cumulative $)."""
    try:
        data = _http_get(f"{KALSHI_MKT}{ticker}/orderbook") or {}
    except Exception:
        return {"levels": [], "last": None, "spread": None}
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    rows = ob.get("yes_dollars") or ob.get("yes") or []
    levels = []
    cum = 0.0
    for row in rows[:limit]:
        if not row or len(row) < 2:
            continue
        price_c = round(_fp(row[0]) * 100, 1)
        size = _fp(row[1])
        level_usd = (price_c / 100.0) * size
        cum += level_usd
        levels.append({
            "price_cents": price_c,
            "size": round(size, 1),
            "total_usd": round(cum, 2),
        })
    m = {}
    try:
        m = (_http_get(KALSHI_MKT + ticker) or {}).get("market") or {}
    except Exception:
        pass
    last = round(_fp(m.get("last_price_dollars")) * 100, 1) if m.get("last_price_dollars") else None
    bid = round(_fp(m.get("yes_bid_dollars")) * 100, 1) if m.get("yes_bid_dollars") else None
    ask = round(_fp(m.get("yes_ask_dollars")) * 100, 1) if m.get("yes_ask_dollars") else None
    spread = round(ask - bid, 1) if bid is not None and ask is not None else None
    return {"levels": levels, "last": last, "bid": bid, "ask": ask, "spread": spread}


def kalshi_market_history(ticker: str, hours: int = 720) -> list[dict]:
    end_ts = int(time.time())
    start_ts = end_ts - hours * 3600
    interval = 60 if hours <= 48 else (1440 if hours > 168 else 60)
    try:
        params = urlencode({
            "market_tickers": ticker,
            "period_interval": interval,
            "start_ts": start_ts,
            "end_ts": end_ts,
        })
        data = _http_get(f"{KALSHI_CANDLES}?{params}") or {}
        mkts = data.get("markets") or []
        cs = (mkts[0] if mkts else {}).get("candlesticks") or []
        pts = []
        for c in cs:
            price = c.get("price") or {}
            close = price.get("close_dollars") or price.get("close")
            if close is None:
                continue
            p = round(_fp(close) * 100 if _fp(close) <= 1 else _fp(close), 1)
            pts.append({"t": int(c.get("end_period_ts", 0)), "p": p})
        return pts
    except Exception:
        return []


def _kalshi_detail_from_trades(raw: list[dict], ticker: str, min_usd: float) -> dict:
    stats = _whale_stats(raw, min_usd)
    return {
        "orderbook": kalshi_orderbook_levels(ticker),
        "recent_trades": _format_trades(raw, min_usd, False)[:80],
        "whale_trades": _format_trades(stats["raw_whales"], min_usd, True)[:80],
        "whale": {k: v for k, v in stats.items() if k != "raw_whales"},
    }


def kalshi_outcome_activity(ticker: str, hours: int = 24, min_usd: float = 500) -> dict:
    min_ts = int(time.time()) - hours * 3600
    raw = kalshi_trades_since(ticker, min_ts, max_pages=_trade_pages_for_hours(hours))
    return _kalshi_detail_from_trades(raw, ticker, min_usd)


def _kalshi_trades_by_ticker(markets: list[dict], hours: int) -> dict[str, list[dict]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    min_ts = int(time.time()) - hours * 3600
    max_pages = _trade_pages_for_hours(hours)
    tickers = [m.get("ticker") for m in markets if m.get("ticker")]
    trades_by: dict[str, list[dict]] = {}

    def _fetch(ticker: str) -> tuple[str, list[dict]]:
        return ticker, kalshi_trades_since(ticker, min_ts, max_pages=max_pages)

    with ThreadPoolExecutor(max_workers=min(8, max(2, len(tickers)))) as pool:
        futs = [pool.submit(_fetch, t) for t in tickers]
        for fut in as_completed(futs):
            ticker, raw = fut.result()
            trades_by[ticker] = raw
    return trades_by


def _kalshi_whale_rows(markets: list[dict], trades_by: dict[str, list[dict]],
                       min_usd: float) -> list[dict]:
    rows = []
    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue
        st = _whale_stats(trades_by.get(ticker, []), min_usd)
        rows.append({
            "label": m.get("label"),
            "slug": m.get("slug") or outcome_slug(m.get("label") or ""),
            "yes_usd": st["yes_usd"],
            "no_usd": st["no_usd"],
            "total_usd": st["total_usd"],
            "trade_count": st["trade_count"],
        })
    return rows


def kalshi_event_whale_rollup(markets: list[dict], hours: int = 24,
                              min_usd: float = 500) -> list[dict]:
    trades_by = _kalshi_trades_by_ticker(markets, hours)
    return _kalshi_whale_rows(markets, trades_by, min_usd)


def kalshi_activity_bundle(markets: list[dict], pick_ticker: str,
                           hours: int = 24, min_usd: float = 500) -> dict:
    """One trade fetch per outcome, parallel book/history — no duplicate API calls."""
    from concurrent.futures import ThreadPoolExecutor

    trades_by = _kalshi_trades_by_ticker(markets, hours)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ob_fut = pool.submit(kalshi_orderbook_levels, pick_ticker)
        hist_fut = pool.submit(kalshi_market_history, pick_ticker, hours)
        orderbook = ob_fut.result()
        history = hist_fut.result()

    event_whales = _kalshi_whale_rows(markets, trades_by, min_usd)
    pick_raw = trades_by.get(pick_ticker, [])
    stats = _whale_stats(pick_raw, min_usd)
    return {
        "event_whales": event_whales,
        "history": history,
        "orderbook": orderbook,
        "recent_trades": _format_trades(pick_raw, min_usd, False)[:80],
        "whale_trades": _format_trades(stats["raw_whales"], min_usd, True)[:80],
        "whale": {k: v for k, v in stats.items() if k != "raw_whales"},
    }


def _poly_trade_usd(t: dict) -> float:
    return _fp(t.get("size")) * _fp(t.get("price"))


def poly_trades_since(condition_id: str, min_ts: int, limit: int = 500) -> list[dict]:
    try:
        data = _http_get(f"{POLY_DATA_TRADES}?{urlencode({'market': condition_id, 'limit': limit})}")
    except Exception:
        return []
    if isinstance(data, list):
        trades = data
    else:
        trades = data.get("trades") or data.get("data") or []
    return [t for t in trades if _parse_ts(str(t.get("timestamp") or t.get("createdAt") or "")) >= min_ts
            or not min_ts]


def poly_orderbook_levels(token_id: str, limit: int = 24) -> dict:
    try:
        data = _http_get(f"{POLY_CLOB_BOOK}?{urlencode({'token_id': token_id})}") or {}
    except Exception:
        return {"levels": [], "asks": [], "bids": [], "last": None, "spread": None}

    def _ladder(rows: list) -> list[dict]:
        out, cum = [], 0.0
        for row in rows[:limit]:
            price_c = round(_fp(row.get("price")) * 100, 1)
            size = _fp(row.get("size"))
            cum += (price_c / 100.0) * size
            out.append({
                "price_cents": price_c,
                "size": round(size, 1),
                "total_usd": round(cum, 2),
            })
        return out

    asks = data.get("asks") or []
    bids = data.get("bids") or []
    ask_levels = _ladder(asks)
    bid_levels = _ladder(bids)
    best_bid = round(_fp(bids[0]["price"]) * 100, 1) if bids else None
    best_ask = round(_fp(asks[0]["price"]) * 100, 1) if asks else None
    spread = round(best_ask - best_bid, 1) if best_bid is not None and best_ask is not None else None
    last = best_ask
    return {
        "levels": ask_levels,
        "asks": ask_levels,
        "bids": bid_levels,
        "bid": best_bid,
        "ask": best_ask,
        "spread": spread,
        "last": last,
    }


def _poly_whale_stats(trades: list[dict], min_usd: float) -> dict:
    yes_usd = no_usd = 0.0
    yes_n = no_n = 0
    whale_trades = []
    for t in trades:
        usd = _poly_trade_usd(t)
        if usd < min_usd:
            continue
        side = (t.get("side") or "").upper()
        outcome = (t.get("outcome") or "").lower()
        is_yes = side == "BUY" and outcome in ("yes", "y") or side == "SELL" and outcome in ("no", "n")
        if not is_yes and side == "BUY":
            is_yes = outcome != "no"
        if is_yes:
            yes_usd += usd
            yes_n += 1
        else:
            no_usd += usd
            no_n += 1
        whale_trades.append(t)
    total = yes_usd + no_usd
    n = yes_n + no_n
    yes_pct = round(100 * yes_usd / total) if total else 0
    no_pct = 100 - yes_pct if total else 0
    sentiment = "bullish" if yes_pct >= 60 else ("bearish" if no_pct >= 60 else "mixed")
    return {
        "total_usd": round(total, 2),
        "trade_count": n,
        "yes_usd": round(yes_usd, 2),
        "no_usd": round(no_usd, 2),
        "yes_pct": yes_pct,
        "no_pct": no_pct,
        "yes_trade_pct": round(100 * yes_n / n) if n else 0,
        "no_trade_pct": 100 - round(100 * yes_n / n) if n else 0,
        "sentiment": sentiment,
        "raw_whales": whale_trades,
    }


def _format_poly_trades(raw: list[dict], min_usd: float, whales_only: bool) -> list[dict]:
    out = []
    for t in raw:
        usd = _poly_trade_usd(t)
        if whales_only and usd < min_usd:
            continue
        side_raw = (t.get("side") or "").upper()
        outcome = (t.get("outcome") or "Yes").lower()
        is_yes = outcome == "yes" or (side_raw == "BUY" and outcome != "no")
        side = "yes" if is_yes else "no"
        price_c = round(_fp(t.get("price")) * 100, 1)
        ts = _parse_ts(str(t.get("timestamp") or t.get("createdAt") or ""))
        out.append({
            "usd": round(usd, 2),
            "side": side,
            "price_cents": price_c,
            "ts": ts,
            "time": str(t.get("timestamp") or ""),
        })
    out.sort(key=lambda x: -x["ts"])
    return out


def poly_market_meta(market_id: str) -> dict:
    now = time.time()
    hit = _POLY_META_CACHE.get(str(market_id))
    if hit and now - hit[0] < _POLY_META_TTL:
        return hit[1]
    meta = {"condition_id": "", "yes_token_id": "", "no_token_id": ""}
    try:
        m = _http_get(POLY_MKT + str(market_id)) or {}
        meta["condition_id"] = m.get("conditionId") or ""
        import json as _json
        toks = _json.loads(m.get("clobTokenIds") or "[]")
        meta["yes_token_id"] = toks[0] if len(toks) > 0 else ""
        meta["no_token_id"] = toks[1] if len(toks) > 1 else ""
    except Exception:
        pass
    _POLY_META_CACHE[str(market_id)] = (now, meta)
    return meta


def poly_event_whale_rollup(markets: list[dict], min_ts: int, min_usd: float,
                            focus_slug: str = "", limit: int = 12) -> list[dict]:
    """Whale totals per outcome; caps work for large multi-outcome events."""
    ranked = sorted(markets, key=lambda m: -(m.get("yes") if m.get("yes") is not None else -1))
    chosen: list[dict] = []
    seen: set[str] = set()
    for m in ranked:
        if len(chosen) >= limit:
            break
        slug = m.get("slug") or outcome_slug(m.get("label") or "")
        if slug in seen:
            continue
        seen.add(slug)
        chosen.append(m)
    if focus_slug and focus_slug not in seen:
        for m in markets:
            if m.get("slug") == focus_slug:
                chosen.append(m)
                break

    def _one(m: dict) -> dict:
        mid = m.get("id")
        raw: list[dict] = []
        if mid:
            try:
                cid = poly_market_meta(str(mid)).get("condition_id") or ""
                if cid:
                    raw = poly_trades_since(cid, min_ts, limit=300)
            except Exception:
                pass
        st = _poly_whale_stats(raw, min_usd)
        return {
            "label": m.get("label"),
            "slug": m.get("slug") or outcome_slug(m.get("label") or ""),
            "yes_usd": st["yes_usd"],
            "no_usd": st["no_usd"],
            "total_usd": st["total_usd"],
            "trade_count": st["trade_count"],
        }

    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_one, m): m for m in chosen}
        for fut in as_completed(futs):
            try:
                out.append(fut.result())
            except Exception:
                pass
    out.sort(key=lambda x: -(x.get("total_usd") or 0))
    return out


def poly_outcome_activity(market_id: str, hours: int = 24, min_usd: float = 500) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    min_ts = int(time.time()) - hours * 3600
    meta = poly_market_meta(str(market_id))
    condition_id = meta.get("condition_id") or ""
    token_id = meta.get("yes_token_id") or ""
    if not condition_id:
        return {
            "orderbook": {"levels": []},
            "recent_trades": [],
            "whale_trades": [],
            "whale": {},
            "meta": meta,
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        raw_fut = pool.submit(poly_trades_since, condition_id, min_ts, 400)
        ob_fut = pool.submit(poly_orderbook_levels, token_id) if token_id else None
        raw = raw_fut.result()
        orderbook = ob_fut.result() if ob_fut else {"levels": []}

    stats = _poly_whale_stats(raw, min_usd)
    return {
        "orderbook": orderbook,
        "recent_trades": _format_poly_trades(raw, min_usd, False)[:80],
        "whale_trades": _format_poly_trades(stats["raw_whales"], min_usd, True)[:80],
        "whale": {k: v for k, v in stats.items() if k != "raw_whales"},
        "meta": meta,
    }


def poly_activity_bundle(markets: list[dict], pick_id: str, pick_slug: str,
                         hours: int = 24, min_usd: float = 500,
                         rollup: bool = True) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    min_ts = int(time.time()) - hours * 3600
    with ThreadPoolExecutor(max_workers=2) as pool:
        detail_fut = pool.submit(poly_outcome_activity, str(pick_id), hours, min_usd)
        whales_fut = pool.submit(
            poly_event_whale_rollup, markets, min_ts, min_usd, pick_slug, 8,
        ) if rollup else None
        detail = detail_fut.result()
        event_whales = whales_fut.result() if whales_fut else []
    return {"event_whales": event_whales, **detail}
