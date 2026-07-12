"""Record Polymarket RTDS Chainlink ticks at five-minute market boundaries.

For crypto Up/Down markets, the reference is the first Chainlink tick at or
after the window start. References are persisted so a process restart does not
replace a previously observed opening value with a later spot approximation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"
WINDOW_MS = 300_000
MAX_VERIFIED_DELAY_MS = int(os.environ.get("POLY_CHAINLINK_MAX_DELAY_MS", "15000"))
REF_PATH = Path(os.environ.get(
    "POLY_CHAINLINK_REF_FILE",
    str(Path(__file__).resolve().parent / "data" / "poly_chainlink_refs.json"),
))
SYMBOLS = tuple(
    item.strip().lower()
    for item in os.environ.get(
        "POLY_CHAINLINK_SYMBOLS",
        "btc/usd,eth/usd,sol/usd,xrp/usd,doge/usd",
    ).split(",")
    if item.strip()
)

_LOCK = threading.RLock()
_REFS: dict[str, dict] = {}
_STARTED = False
_CONNECTED = False
_CONNECTED_SINCE_MS = 0
_LAST_MESSAGE_MS = 0


def _key(coin: str, start: int) -> str:
    return f"{coin.upper()}|{int(start)}"


def _load() -> None:
    global _REFS
    try:
        raw = json.loads(REF_PATH.read_text())
        if isinstance(raw, dict):
            _REFS = raw
            for row in _REFS.values():
                # Older records did not prove the recorder was connected before
                # T0 and therefore cannot be promoted to verified references.
                if row.get("connected_before_window") is not True:
                    row["verified"] = False
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _REFS = {}


def _persist_locked() -> None:
    cutoff = int(time.time()) - 3 * 86400
    current = {
        key: value
        for key, value in _REFS.items()
        if int(value.get("window_start") or 0) >= cutoff
    }
    _REFS.clear()
    _REFS.update(current)
    try:
        REF_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = REF_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(_REFS, separators=(",", ":"), sort_keys=True))
        temp.replace(REF_PATH)
    except OSError as exc:
        log.warning("Could not persist Polymarket Chainlink references: %s", exc)


def _record_tick(symbol: str, timestamp_ms: int, value: float) -> None:
    global _LAST_MESSAGE_MS
    normalized = str(symbol or "").lower()
    if not normalized.endswith("/usd") or timestamp_ms <= 0 or value <= 0:
        return
    coin = normalized.split("/", 1)[0].upper()
    start_ms = timestamp_ms // WINDOW_MS * WINDOW_MS
    start = start_ms // 1000
    delay_ms = timestamp_ms - start_ms
    connected_before_window = 0 < _CONNECTED_SINCE_MS <= start_ms
    key = _key(coin, start)
    row = {
        "coin": coin,
        "window_start": start,
        "price": float(value),
        "tick_timestamp_ms": int(timestamp_ms),
        "delay_ms": int(delay_ms),
        "source": "polymarket_rtds_chainlink",
        "connected_before_window": connected_before_window,
        "verified": connected_before_window and 0 <= delay_ms <= MAX_VERIFIED_DELAY_MS,
        "recorded_at_ms": int(time.time() * 1000),
    }
    with _LOCK:
        _LAST_MESSAGE_MS = max(_LAST_MESSAGE_MS, timestamp_ms)
        existing = _REFS.get(key)
        # Historical snapshots may arrive after live updates. Preserve the
        # earliest tick at/after T0, not whichever message arrived first.
        if existing and int(existing.get("tick_timestamp_ms") or 0) <= timestamp_ms:
            return
        _REFS[key] = row
        _persist_locked()


def _consume_payload(payload) -> None:
    if isinstance(payload, list):
        for item in payload:
            _consume_payload(item)
        return
    if not isinstance(payload, dict):
        return
    symbol = payload.get("symbol")
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _record_tick(
                    symbol or item.get("symbol"),
                    int(item.get("timestamp") or 0),
                    float(item.get("value") or 0),
                )
        return
    if symbol and payload.get("timestamp") is not None:
        try:
            _record_tick(
                symbol,
                int(payload["timestamp"]),
                float(payload.get("value") or 0),
            )
        except (TypeError, ValueError):
            return


async def _ping(ws) -> None:
    while True:
        await asyncio.sleep(5)
        await ws.send("PING")


async def _session(websockets) -> None:
    global _CONNECTED, _CONNECTED_SINCE_MS
    subscriptions = [
        {
            "topic": TOPIC,
            "type": "*",
            "filters": json.dumps({"symbol": symbol}, separators=(",", ":")),
        }
        for symbol in SYMBOLS
    ]
    async with websockets.connect(
        WS_URL,
        ping_interval=None,
        open_timeout=10,
        close_timeout=5,
        max_size=4 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({"action": "subscribe", "subscriptions": subscriptions}))
        _CONNECTED_SINCE_MS = int(time.time() * 1000)
        _CONNECTED = True
        ping_task = asyncio.create_task(_ping(ws))
        try:
            async for raw in ws:
                if raw in ("PING", "PONG"):
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(message, list):
                    for item in message:
                        if isinstance(item, dict) and item.get("topic") == TOPIC:
                            _consume_payload(item.get("payload"))
                elif message.get("topic") == TOPIC:
                    _consume_payload(message.get("payload"))
        finally:
            _CONNECTED = False
            ping_task.cancel()


async def _run() -> None:
    try:
        import websockets
    except ImportError:
        log.warning("Polymarket Chainlink recorder disabled: websockets not installed")
        return
    while True:
        try:
            await _session(websockets)
        except Exception as exc:
            global _CONNECTED
            _CONNECTED = False
            log.warning("Polymarket Chainlink recorder reconnecting: %s", exc)
            await asyncio.sleep(2)


def start() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        _load()
    threading.Thread(
        target=lambda: asyncio.run(_run()),
        daemon=True,
        name="poly-chainlink-t0",
    ).start()


def reference(coin: str, start_epoch: int) -> dict | None:
    start()
    with _LOCK:
        row = _REFS.get(_key(coin, start_epoch))
        return dict(row) if row else None


def status() -> dict:
    start()
    with _LOCK:
        return {
            "connected": _CONNECTED,
            "connected_since_ms": _CONNECTED_SINCE_MS,
            "references": len(_REFS),
            "last_message_ms": _LAST_MESSAGE_MS,
            "max_verified_delay_ms": MAX_VERIFIED_DELAY_MS,
        }

