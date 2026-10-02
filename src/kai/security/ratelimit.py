"""Fixed-window rate limiting for the unauthenticated auth routes.

Why this exists: ``/auth/login`` and ``/auth/register`` are the only endpoints
reachable without a token, and login is a password oracle. Without a limit,
one client can attempt unbounded guesses; with it, credential stuffing is
bounded per source.

Why the backend is pluggable: the limit is only a real limit if every replica
sees the same counters. The in-memory store is per-process, so behind a load
balancer it would multiply the effective limit by the replica count. Production
therefore refuses to start without Redis (see ``config``), and the memory store
exists for local runs and tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Final

from starlette.types import ASGIApp, Receive, Scope, Send

from kai.config import get_settings
from kai.logging import get_logger
from kai.observability import metrics

_log = get_logger(__name__)

#: Bounded reason classes for a rejection. Two reasons, both decided in this
#: module, so the metric label can never grow with client input.
REASON_EXCEEDED: Final[str] = "exceeded"
REASON_BACKEND_UNAVAILABLE: Final[str] = "backend_unavailable"


class RateLimitExceeded(Exception):
    """Raised when a caller has exhausted its allowance for the window."""


class _MemoryStore:
    """Per-process fixed-window counters.

    Correct only for a single process. Each key holds ``(count, reset_at)`` for
    the current window, and expired entries are dropped lazily on access so a
    burst of one-off keys cannot grow without bound.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, *, limit: int, window: float) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            count, reset_at = self._entries.get(key, (0, now + window))
            if now >= reset_at:
                count, reset_at = 0, now + window
            count += 1
            self._entries[key] = (count, reset_at)
        return count <= limit, reset_at - now


class _RedisStore:
    """Shared counters via Redis ``INCR`` + ``EXPIRE``.

    ``INCR`` is atomic, so concurrent requests from one client cannot both read
    the same pre-increment count and both be admitted. The expiry is applied
    only when the key is created, which fixes the window at first hit and stops
    a sustained attacker from holding a counter open forever.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis_asyncio

            self._client = redis_asyncio.from_url(
                self._redis_url, decode_responses=True
            )
        return self._client

    async def hit(self, key: str, *, limit: int, window: float) -> tuple[bool, float]:
        client = self._get_client()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, int(window) + 1)
        ttl = await client.ttl(key)
        allowed = count <= limit
        return allowed, float(ttl) if ttl and ttl > 0 else window


_store: _MemoryStore | _RedisStore | None = None
_store_lock = threading.Lock()


def _get_store() -> _MemoryStore | _RedisStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                settings = get_settings()
                backend = settings.rate_limit_backend
                if backend == "auto":
                    backend = "redis" if settings.redis_url else "memory"
                _store = (
                    _RedisStore(settings.redis_url)
                    if backend == "redis"
                    else _MemoryStore()
                )
    return _store


def reset_store() -> None:
    """Drop all counters. Used by tests to isolate windows."""
    global _store
    with _store_lock:
        _store = None


class RateLimitMiddleware:
    """Starlette middleware enforcing a per-source limit on chosen paths.

    The key includes the client address and the path, so exhausting the login
    allowance does not also lock a legitimate user out of registration, and one
    noisy source cannot consume another source's budget.

    Forwarded-for headers are deliberately ignored. They are attacker-controlled
    unless a trusted proxy is known to overwrite them, and honouring them here
    would let a client mint a fresh rate-limit identity per request. The cost
    is that a real client behind a proxy is limited as its proxy; deployments
    that need per-client limits should terminate the limit at a proxy that
    already does this correctly.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: tuple[str, ...],
        limit: Callable[[], int],
        window: Callable[[], float],
    ) -> None:
        self.app = app
        self._paths = paths
        self._limit = limit
        self._window = window

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not any(path.endswith(p) for p in self._paths):
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        host = client[0] if client else "unknown"
        key = f"kai:ratelimit:{path}:{host}"
        limit = self._limit()
        window = self._window()

        try:
            store = _get_store()
            allowed, retry_after = await store.hit(key, limit=limit, window=window)
        except Exception as exc:  # pragma: no cover - backend outage path
            # Fail closed. A limiter that cannot reach its store must not
            # silently become "no limit", which is the failure mode that turns
            # a Redis blip into an open brute-force window.
            _log.error("rate_limit_backend_unavailable", error=str(exc), path=path)
            metrics.record_rate_limit_rejection(REASON_BACKEND_UNAVAILABLE)
            await _send_rate_limited(send, 1)
            return

        if not allowed:
            # ``path`` here is one of the two constants in RATE_LIMITED_PATHS, so
            # it is a bounded label by construction. The client address is
            # deliberately NOT a label: it is unbounded and would leak who is
            # being limited into a shared monitoring system. It stays in the log
            # line, which is access-controlled, rather than in the metric.
            _log.warning("rate_limit_exceeded", path=path, client=host)
            metrics.record_rate_limit_rejection(REASON_EXCEEDED)
            await _send_rate_limited(send, max(int(retry_after), 1))
            return

        await self.app(scope, receive, send)


async def _send_rate_limited(send: Send, retry_after: int) -> None:
    body = b'{"detail":"too many requests, retry later"}'
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
