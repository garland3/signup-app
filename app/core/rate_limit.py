"""Tiny in-memory rate limiter.

Fixed-window counters keyed by (bucket, identifier). Good enough for a
single-replica deployment; swap for Redis if you run more than one
worker and need a shared view. Routes call ``check()`` to consume a
token and get back True (allowed) / False (limited).
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class RateLimiter:
    def __init__(self) -> None:
        # {(bucket, key): (window_start_epoch, count)}
        self._buckets: dict[tuple[str, str], tuple[float, int]] = defaultdict(
            lambda: (0.0, 0)
        )
        self._lock = Lock()

    def check(self, bucket: str, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            start, count = self._buckets[(bucket, key)]
            if now - start >= window_seconds:
                self._buckets[(bucket, key)] = (now, 1)
                return True
            if count >= limit:
                return False
            self._buckets[(bucket, key)] = (start, count + 1)
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Process-wide default limiter
limiter = RateLimiter()
