"""Cached HTTP GET with rate limiting and single-flight coalescing."""

from __future__ import annotations

import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import rate_limit

_HTTP_CACHE: dict[str, tuple[float, object]] = {}
_HTTP_INFLIGHT: dict[str, threading.Event] = {}
_HTTP_INFLIGHT_LOCK = threading.Lock()


def _http_fetch(url: str, retries: int) -> object:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "crypto-arb-app/1.0"})
    for attempt in range(retries):
        rate_limit.acquire(url)
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                _HTTP_CACHE[url] = (time.time(), data)
                return data
        except HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(rate_limit.note_429(url, e.headers.get("Retry-After")))
                continue
            raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


def http_get(url: str, retries: int = 4, ttl: float = 8.0) -> object:
    now = time.time()
    hit = _HTTP_CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]

    with _HTTP_INFLIGHT_LOCK:
        ev = _HTTP_INFLIGHT.get(url)
        leader = ev is None
        if leader:
            ev = threading.Event()
            _HTTP_INFLIGHT[url] = ev
    if not leader:
        ev.wait(timeout=35)
        hit = _HTTP_CACHE.get(url)
        if hit and time.time() - hit[0] < ttl + 30:
            return hit[1]
        return _http_fetch(url, retries)

    try:
        return _http_fetch(url, retries)
    finally:
        with _HTTP_INFLIGHT_LOCK:
            _HTTP_INFLIGHT.pop(url, None)
        ev.set()
