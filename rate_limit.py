"""Shared outbound rate limiting for third-party APIs (Polymarket especially).

Everything the app fetches from Polymarket runs from a single Heroku dyno = a single
IP, so bursts from background loops + per-user requests trip Polymarket's per-IP limit.
This module provides one process-wide token bucket per host so the *total* request rate
stays under the threshold no matter how many threads call, plus a helper to honor the
``Retry-After`` header Polymarket returns on 429.
"""
from __future__ import annotations

import os
import threading
import time


class TokenBucket:
    """Classic token bucket: sustains `rate` requests/sec with room for `burst`.

    ``acquire()`` blocks just long enough to stay within budget, so callers never need
    to reason about timing — they just call it before each outbound request.
    """

    def __init__(self, rate: float, burst: float):
        self.rate = float(rate)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.05
            time.sleep(min(wait, 5.0))

    def penalize(self, seconds: float) -> None:
        """Drain the bucket and push the refill clock forward after a 429 so the whole
        process backs off — not just the thread that got limited."""
        with self._lock:
            self._tokens = 0.0
            self._updated = time.monotonic() + max(0.0, seconds)


# Per-host buckets. Polymarket's public limit is undocumented but ~a handful of req/s;
# default to a conservative shared 6/s with a small burst, tunable via env.
_BUCKETS: dict[str, TokenBucket] = {}
_BUCKETS_LOCK = threading.Lock()

_DEFAULTS = {
    "polymarket": (float(os.environ.get("POLY_RATE", "6")),
                   float(os.environ.get("POLY_BURST", "12"))),
    "kalshi": (float(os.environ.get("KALSHI_RATE", "8")),
               float(os.environ.get("KALSHI_BURST", "16"))),
}


def _host_key(url: str) -> str | None:
    if "polymarket.com" in url:
        return "polymarket"
    if "kalshi.com" in url or "elections.kalshi" in url:
        return "kalshi"
    return None


def bucket_for(url: str) -> TokenBucket | None:
    key = _host_key(url)
    if key is None:
        return None
    b = _BUCKETS.get(key)
    if b is None:
        with _BUCKETS_LOCK:
            b = _BUCKETS.get(key)
            if b is None:
                rate, burst = _DEFAULTS.get(key, (6.0, 12.0))
                b = TokenBucket(rate, burst)
                _BUCKETS[key] = b
    return b


def acquire(url: str) -> None:
    """Block until it's within budget to make one request to `url`'s host (no-op for
    hosts we don't rate-limit)."""
    b = bucket_for(url)
    if b is not None:
        b.acquire()


def note_429(url: str, retry_after: str | None) -> float:
    """Record a 429 for `url`'s host and return how long to sleep before retrying,
    honoring the server's ``Retry-After`` header when present."""
    delay = parse_retry_after(retry_after)
    if delay is None:
        delay = 2.0
    b = bucket_for(url)
    if b is not None:
        b.penalize(delay)
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds form). Returns None if absent/unparsable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        # HTTP-date form is rare here; fall back to a fixed pause rather than parse it.
        return None
