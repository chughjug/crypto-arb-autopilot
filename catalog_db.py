"""SQLite-backed catalog storage — full market lists without RAM cache."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

DEFAULT_PATH = os.environ.get(
    "CATALOG_DB_PATH",
    "/tmp/catalog.db" if os.environ.get("DYNO") else str(
        Path(__file__).parent / "data" / "catalog.db"
    ),
)


def _db_path() -> str:
    path = Path(DEFAULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    with _lock:
        c = _connect()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                source TEXT NOT NULL,
                event_id TEXT NOT NULL,
                title TEXT NOT NULL,
                search_text TEXT NOT NULL,
                volume INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                PRIMARY KEY (source, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
            CREATE INDEX IF NOT EXISTS idx_events_search ON events(search_text);
            CREATE INDEX IF NOT EXISTS idx_events_volume ON events(volume DESC);
        """)
        c.commit()

        # One-time migration to enrich existing database rows for 5-min/15-min crypto and up-down markets
        try:
            flag = c.execute("SELECT value FROM meta WHERE key = 'mig_short_crypto'").fetchone()
            if not flag:
                import re
                rows = c.execute("SELECT event_id, source, data FROM events").fetchall()
                for r in rows:
                    try:
                        ev = json.loads(r["data"])
                    except Exception:
                        continue
                    title = str(ev.get("title", "") or "").lower()
                    subtitle = str(ev.get("subtitle", "") or "").lower()
                    category = str(ev.get("category", "") or "").lower()
                    ticker = str(ev.get("ticker", "") or "").lower()
                    blob = f"{title} {subtitle} {category} {ticker}"
                    words = set(re.findall(r"[a-z0-9]+", blob))
                    is_short = (
                        "up or down" in blob or
                        "5 min" in blob or
                        "15 min" in blob or
                        any(w in words for w in ("5m", "15m", "5min", "15min", "5mins", "15mins"))
                    )
                    if is_short:
                        blob += " 5 min 5-min 5m 15 min 15-min 15m crypto short-term short term"
                    c.execute("UPDATE events SET search_text = ? WHERE event_id = ? AND source = ?", (blob, r["event_id"], r["source"]))
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('mig_short_crypto', '1')")
                c.commit()
        except Exception as err:
            print("Migration error:", err)



def _search_blob(ev: dict) -> str:
    title = str(ev.get("title", "") or "").lower()
    subtitle = str(ev.get("subtitle", "") or "").lower()
    category = str(ev.get("category", "") or "").lower()
    ticker = str(ev.get("ticker", "") or "").lower()
    
    blob = f"{title} {subtitle} {category} {ticker}"
    
    import re
    words = set(re.findall(r"[a-z0-9]+", blob))
    
    # Check if this is a short-term crypto market (using whole word matches for codes)
    is_short = (
        "up or down" in blob or
        "5 min" in blob or
        "15 min" in blob or
        any(w in words for w in ("5m", "15m", "5min", "15min", "5mins", "15mins"))
    )
    if is_short:
        blob += " 5 min 5-min 5m 15 min 15-min 15m crypto short-term short term"
        
    return blob


def clear_source(source: str) -> None:
    with _lock:
        c = _connect()
        c.execute("DELETE FROM events WHERE source = ?", (source,))
        c.commit()


def _event_key(source: str, ev: dict) -> str:
    if source == "kalshi":
        return ev.get("ticker") or ""
    return ev.get("id") or ev.get("slug") or str(ev.get("url", "").split("/event/")[-1] or "")


def upsert_batch(source: str, events: list[dict]) -> list[str]:
    """Insert/replace events. Returns the stored event_ids (so a refresh can
    upsert then prune stale rows, instead of clearing the table up front)."""
    if not events:
        return []
    rows = []
    eids: list[str] = []
    for ev in events:
        eid = _event_key(source, ev)
        if not eid:
            continue
        eids.append(eid)
        rows.append((
            source,
            eid,
            ev.get("title", ""),
            _search_blob(ev),
            int(ev.get("volume") or 0),
            json.dumps(ev, separators=(",", ":")),
        ))
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT OR REPLACE INTO events (source, event_id, title, search_text, volume, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
    return eids


def prune_source(source: str, keep_ids: set[str]) -> int:
    """Delete rows for `source` whose event_id is not in keep_ids.

    Used at the END of a refresh (after upserting fresh rows) so readers never
    see an empty/partial table mid-refresh. Refuses to prune when keep_ids is
    empty — a failed/empty fetch must not wipe the existing catalog.
    """
    if not keep_ids:
        return 0
    with _lock:
        c = _connect()
        existing = {
            r["event_id"]
            for r in c.execute("SELECT event_id FROM events WHERE source = ?", (source,))
        }
        stale = existing - keep_ids
        if stale:
            c.executemany(
                "DELETE FROM events WHERE source = ? AND event_id = ?",
                [(source, e) for e in stale],
            )
            c.commit()
        return len(stale)


def set_meta(stats: dict) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("stats", json.dumps(stats)),
        )
        c.commit()


def get_meta() -> dict:
    with _lock:
        c = _connect()
        row = c.execute("SELECT value FROM meta WHERE key = 'stats'").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return {}


def count(source: str | None = None) -> int:
    with _lock:
        c = _connect()
        if source:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM events WHERE source = ?", (source,)
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    return int(row["n"]) if row else 0


def load_all(source: str) -> list[dict]:
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT data FROM events WHERE source = ? ORDER BY volume DESC", (source,)
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            out.append(json.loads(row["data"]))
        except json.JSONDecodeError:
            continue
    return out


def load_lightweight(source: str) -> list[dict]:
    """Title/subtitle/id only — for cross-platform matching without full catalog RAM."""
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT event_id, title, json_extract(data, '$.subtitle') AS subtitle"
            " FROM events WHERE source = ? ORDER BY volume DESC",
            (source,),
        )
        rows = cur.fetchall()
    return [
        {
            "event_id": row["event_id"],
            "title": row["title"] or "",
            "subtitle": row["subtitle"] or "",
        }
        for row in rows
    ]


def load_by_ids(source: str, event_ids: list[str]) -> list[dict]:
    if not event_ids:
        return []
    out: list[dict] = []
    chunk_size = 400
    for off in range(0, len(event_ids), chunk_size):
        chunk = event_ids[off: off + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        with _lock:
            c = _connect()
            cur = c.execute(
                f"SELECT data FROM events WHERE source = ? AND event_id IN ({placeholders})",
                [source, *chunk],
            )
            rows = cur.fetchall()
        for row in rows:
            try:
                out.append(json.loads(row["data"]))
            except json.JSONDecodeError:
                continue
    return out


def get_one(source: str, key: str) -> dict | None:
    """Fetch a single event by its public key: ticker for Kalshi, slug (or id)
    for Polymarket / crypto.com. Used by the market detail page."""
    if not key:
        return None
    with _lock:
        c = _connect()
        if source == "kalshi":
            row = c.execute(
                "SELECT data FROM events WHERE source = 'kalshi' AND event_id = ?",
                (key,),
            ).fetchone()
            if not row:
                # Fallback: find parent event by market/contract ticker
                row = c.execute(
                    "SELECT data FROM events WHERE source = 'kalshi' AND event_id = ("
                    "  SELECT event_id FROM events, json_each(json_extract(events.data, '$.markets'))"
                    "  WHERE source = 'kalshi' AND json_extract(value, '$.ticker') = ? LIMIT 1"
                    ")",
                    (key,),
                ).fetchone()
        else:
            # crypto.com events keep their slug in $.ticker, Polymarket in $.slug.
            row = c.execute(
                "SELECT data FROM events WHERE source = ?"
                " AND (event_id = ? OR json_extract(data, '$.slug') = ?"
                "      OR json_extract(data, '$.ticker') = ?)",
                (source, key, key, key),
            ).fetchone()
            if not row:
                # Fallback: find parent event by market/contract ID
                row = c.execute(
                    "SELECT data FROM events WHERE source = ? AND event_id = ("
                    "  SELECT event_id FROM events, json_each(json_extract(events.data, '$.markets'))"
                    "  WHERE source = ? AND json_extract(value, '$.id') = ? LIMIT 1"
                    ")",
                    (source, source, key),
                ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except json.JSONDecodeError:
        return None


def search(source: str, query: str, limit: int = 30) -> list[dict]:
    q = query.lower().strip()
    if not q:
        return []
    words = [w for w in q.split() if len(w) > 2]
    with _lock:
        c = _connect()
        if words:
            clause = " AND ".join("search_text LIKE ?" for _ in words)
            params: list = [source] + [f"%{w}%" for w in words]
            cur = c.execute(
                f"SELECT data FROM events WHERE source = ? AND {clause}"
                f" ORDER BY volume DESC, length(json_extract(data, '$.title')) ASC, json_array_length(json_extract(data, '$.markets')) DESC LIMIT ?",
                params + [limit],
            )
        else:
            cur = c.execute(
                "SELECT data FROM events WHERE source = ? AND search_text LIKE ?"
                " ORDER BY volume DESC, length(json_extract(data, '$.title')) ASC, json_array_length(json_extract(data, '$.markets')) DESC LIMIT ?",
                (source, f"%{q}%", limit),
            )
        rows = cur.fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            out.append(json.loads(row["data"]))
        except json.JSONDecodeError:
            continue
    return out


_EXPLORE_SQL = """
SELECT
  event_id,
  source,
  title,
  volume,
  json_extract(data, '$.subtitle') AS subtitle,
  json_extract(data, '$.category') AS category,
  json_extract(data, '$.image') AS image,
  json_extract(data, '$.url') AS url,
  json_extract(data, '$.slug') AS slug,
  COALESCE(json_extract(data, '$.ticker'), event_id) AS ticker,
  COALESCE(json_array_length(json_extract(data, '$.markets')), 0) AS markets,
  json_extract(data, '$.markets[0].label') AS m0_label,
  json_extract(data, '$.markets[0].last')  AS m0_last,
  json_extract(data, '$.markets[0].bid')   AS m0_bid,
  json_extract(data, '$.markets[0].ask')   AS m0_ask,
  json_extract(data, '$.markets[0].image') AS m0_img,
  json_extract(data, '$.markets[1].label') AS m1_label,
  json_extract(data, '$.markets[1].last')  AS m1_last,
  json_extract(data, '$.markets[1].bid')   AS m1_bid,
  json_extract(data, '$.markets[1].ask')   AS m1_ask,
  json_extract(data, '$.markets[1].image') AS m1_img,
  json_extract(data, '$.markets[2].label') AS m2_label,
  json_extract(data, '$.markets[2].last')  AS m2_last,
  json_extract(data, '$.markets[2].bid')   AS m2_bid,
  json_extract(data, '$.markets[2].ask')   AS m2_ask,
  json_extract(data, '$.markets[2].image') AS m2_img,
  json_extract(data, '$.markets[3].label') AS m3_label,
  json_extract(data, '$.markets[3].last')  AS m3_last,
  json_extract(data, '$.markets[3].bid')   AS m3_bid,
  json_extract(data, '$.markets[3].ask')   AS m3_ask,
  json_extract(data, '$.markets[3].image') AS m3_img
FROM events
WHERE (? = 'all' OR source = ?)
  AND volume >= ?
  AND (? = '' OR search_text LIKE ?)
ORDER BY volume DESC
LIMIT ? OFFSET ?
"""


def _yes_from_row(row: sqlite3.Row) -> int | None:
    yes = row["m0_last"]
    bid, ask = row["m0_bid"], row["m0_ask"]
    if yes is None and bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if not (b == 0 and a == 100):
                yes = round((b + a) / 2)
        except (TypeError, ValueError):
            pass
    if yes is None:
        return None
    try:
        return int(yes)
    except (TypeError, ValueError):
        return None


def _outcome_price(last, bid, ask) -> int | None:
    if last is not None:
        try:
            return int(last)
        except (TypeError, ValueError):
            pass
    if bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if not (b == 0 and a == 100):
                return round((b + a) / 2)
        except (TypeError, ValueError):
            pass
    return None


def _slim_row(row: sqlite3.Row) -> dict:
    outcomes = []
    for i in range(4):
        label = row[f"m{i}_label"]
        if not label:
            break
        price = _outcome_price(row[f"m{i}_last"], row[f"m{i}_bid"], row[f"m{i}_ask"])
        img = row[f"m{i}_img"] or ""
        outcomes.append({"label": label, "yes": price, "image": img})
    return {
        "venue": row["source"],
        "title": row["title"] or "",
        "subtitle": row["subtitle"] or "",
        "category_raw": row["category"] or "",
        "volume": int(row["volume"] or 0),
        "image": row["image"] or "",
        "url": row["url"] or "",
        "slug": row["slug"] or "",
        "ticker": row["ticker"] or row["event_id"],
        "markets": int(row["markets"] or 0),
        "yes": _yes_from_row(row),
        "outcomes": outcomes,
    }


def explore_page(venue: str = "all", min_volume: int = 0, q: str = "",
                 limit: int = 48, offset: int = 0) -> list[dict]:
    """Fast paginated explore rows — no full JSON parse."""
    ql = q.lower().strip()
    like = f"%{ql}%" if ql else ""
    with _lock:
        c = _connect()
        rows = c.execute(
            _EXPLORE_SQL,
            (venue, venue, min_volume, ql, like, limit, offset),
        ).fetchall()
    return [_slim_row(r) for r in rows]


def explore_count(venue: str = "all", min_volume: int = 0, q: str = "") -> int:
    ql = q.lower().strip()
    like = f"%{ql}%" if ql else ""
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT COUNT(*) AS n FROM events"
            " WHERE (? = 'all' OR source = ?) AND volume >= ?"
            " AND (? = '' OR search_text LIKE ?)",
            (venue, venue, min_volume, ql, like),
        ).fetchone()
    return int(row["n"]) if row else 0


def load_explore_slim() -> list[dict]:
    """All explore rows for in-memory facet/sort (SQL json_extract only)."""
    with _lock:
        c = _connect()
        rows = c.execute(
            """
            SELECT
              event_id, source, title, volume,
              json_extract(data, '$.subtitle') AS subtitle,
              json_extract(data, '$.category') AS category,
              json_extract(data, '$.image') AS image,
              json_extract(data, '$.url') AS url,
              json_extract(data, '$.slug') AS slug,
              COALESCE(json_extract(data, '$.ticker'), event_id) AS ticker,
              COALESCE(json_array_length(json_extract(data, '$.markets')), 0) AS markets,
              json_extract(data, '$.markets[0].label') AS m0_label,
              json_extract(data, '$.markets[0].last')  AS m0_last,
              json_extract(data, '$.markets[0].bid')   AS m0_bid,
              json_extract(data, '$.markets[0].ask')   AS m0_ask,
              json_extract(data, '$.markets[0].image') AS m0_img,
              json_extract(data, '$.markets[1].label') AS m1_label,
              json_extract(data, '$.markets[1].last')  AS m1_last,
              json_extract(data, '$.markets[1].bid')   AS m1_bid,
              json_extract(data, '$.markets[1].ask')   AS m1_ask,
              json_extract(data, '$.markets[1].image') AS m1_img,
              json_extract(data, '$.markets[2].label') AS m2_label,
              json_extract(data, '$.markets[2].last')  AS m2_last,
              json_extract(data, '$.markets[2].bid')   AS m2_bid,
              json_extract(data, '$.markets[2].ask')   AS m2_ask,
              json_extract(data, '$.markets[2].image') AS m2_img,
              json_extract(data, '$.markets[3].label') AS m3_label,
              json_extract(data, '$.markets[3].last')  AS m3_last,
              json_extract(data, '$.markets[3].bid')   AS m3_bid,
              json_extract(data, '$.markets[3].ask')   AS m3_ask,
              json_extract(data, '$.markets[3].image') AS m3_img
            FROM events ORDER BY volume DESC
            """
        ).fetchall()
    return [_slim_row(r) for r in rows]


def load_crypto_events() -> list[dict]:
    """Load all crypto events from database."""
    with _lock:
        c = _connect()
        rows = c.execute(
            """
            SELECT data FROM events
            WHERE search_text LIKE '%crypto%'
            ORDER BY volume DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data"]))
        except Exception:
            continue
    return out


def stats() -> dict:
    meta = get_meta()
    age = None
    if meta.get("cached_at"):
        age = round(time.time() - float(meta["cached_at"]), 1)
    return {
        **meta,
        "kalshi_events": count("kalshi"),
        "poly_events": count("polymarket"),
        "cryptocom_events": count("cryptocom"),
        "db_path": _db_path(),
        "cache_age_seconds": age,
    }


init_db()
