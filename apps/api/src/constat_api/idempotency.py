"""Idempotency-Key support for write endpoints.

A client that retries a request (because the first one timed out at
the LB, network blip, etc.) without an Idempotency-Key can trigger the
operation twice. For /collect/aws this is mostly saved by the
source_runs partial unique index, but for /insights/run it creates
duplicate insight_runs and double-emits insights.

V1 fix: when the client sends `Idempotency-Key: <opaque-string>`:
- First request with that key: run, cache the response, return it.
- Retry within TTL (default 5 min) with the same key: return the
  cached response, do NOT re-run.
- The body is ignored on retry — same key = same response (industry
  standard, Stripe-style). Clients should use unique keys per logical
  operation.

Storage: in-process dict. V1 single-process, so this works. Lost on
restart — that's fine for a 5min TTL. V2: swap to a Postgres table or
Redis when we go multi-replica.

Scope namespacing: the cache key is namespaced per endpoint
("collect_aws:<key>", "insights_run:<key>") so a key reused across
endpoints doesn't collide.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, cast

logger = logging.getLogger(__name__)

# Default TTL: 5 minutes. Long enough to cover a typical retry storm
# (network blip -> immediate retry, then 30s later, then 60s, etc.),
# short enough that a forgotten key doesn't pin a stale response
# indefinitely.
DEFAULT_TTL = timedelta(minutes=5)

# Cap on cache size to bound memory. When the cap is hit, the oldest
# entries are evicted (FIFO by insert order). Realistically with 5min
# TTL and modest traffic, this is never hit.
MAX_ENTRIES = 1024


class IdempotencyCache:
    """Thread-safe in-process cache for idempotent responses.

    Uses a dict + a deque for FIFO eviction. Lazy expiry: a `get`
    checks the timestamp and drops stale entries. No background
    sweeper (the cache is small enough that the lazy approach is
    fine).
    """

    def __init__(self, ttl: timedelta = DEFAULT_TTL, max_entries: int = MAX_ENTRIES):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: dict[str, tuple[datetime, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        """Return the cached JSON body for `key`, or None if missing/expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, body = entry
            if datetime.now(tz=UTC) - ts > self.ttl:
                # Lazy expiry: drop and return None
                del self._store[key]
                return None
            return body

    def put(self, key: str, body: str) -> None:
        """Cache a response body for `key`. Evicts oldest if over capacity."""
        with self._lock:
            # Drop expired first (cheap, in case many are stale)
            now = datetime.now(tz=UTC)
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self.ttl]
            for k in expired:
                self._store.pop(k, None)
            # Evict oldest if still over capacity. The dict preserves
            # insertion order in Python 3.7+, so `next(iter(...))` is the
            # first-inserted key (FIFO).
            while len(self._store) >= self.max_entries:
                oldest = next(iter(self._store))
                self._store.pop(oldest, None)
            self._store[key] = (now, body)

    def clear(self) -> None:
        """Test helper. Wipes the cache."""
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton. V1 single-process; no need for a per-app
# instance. Tests can call `idempotency_cache.clear()` between runs.
idempotency_cache = IdempotencyCache()


def make_cache_key(scope: str, key: str) -> str:
    """Build a namespaced cache key.

    Two endpoints using the same Idempotency-Key (e.g. a UUID the
    client reuses across calls) must not share a cache entry.
    Scope namespacing prevents that.
    """
    return f"{scope}:{key}"


def get_cached_or_none(scope: str, key: str) -> dict[str, Any] | None:
    """Return the cached response as a dict, or None if not cached.

    Returns None for both "no entry" and "expired" — same behavior.
    The `scope` and `key` together form the namespaced cache key.
    """
    full_key = make_cache_key(scope, key)
    body = idempotency_cache.get(full_key)
    if body is None:
        return None
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError:
        logger.warning("Idempotency cache: corrupted body for key %s", full_key)
        return None


def cache_response(scope: str, key: str, response_dict: dict[str, Any]) -> None:
    """Store a response dict under the given namespaced key."""
    full_key = make_cache_key(scope, key)
    idempotency_cache.put(full_key, json.dumps(response_dict, default=str))
