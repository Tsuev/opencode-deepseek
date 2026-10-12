"""
Lightweight in-process rate limiting.

A sliding-window log limiter, keyed by client IP. No external dependencies and
no Redis — fine for the single-process server this project runs. If you ever
scale to multiple workers you'll want a shared store instead, since each worker
keeps its own counters.

    limiter = RateLimiter(limit=30, window=60)        # 30 requests / minute
    allowed, remaining, retry_after = limiter.hit("1.2.3.4", now=time.time())
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimiter:
    """Sliding-window request counter shared across requests (thread-safe)."""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10000):
        if limit <= 0 or window <= 0 or max_keys <= 0:
            raise ValueError("Rate limit, window and max_keys must be positive")
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: OrderedDict[str, Deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str, now: float) -> Tuple[bool, int, float]:
        """Record a request for `key`. Returns (allowed, remaining, retry_after).

        `retry_after` is seconds until the oldest in-window hit expires (0 when
        the request is allowed).
        """
        cutoff = now - self.window
        with self._lock:
            # Ordered by the last admitted hit: remove expired identities even
            # if those clients never return, without scanning all active keys.
            while self._hits:
                oldest = next(iter(self._hits))
                if self._hits[oldest][-1] > cutoff:
                    break
                self._hits.popitem(last=False)
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    oldest_hits = next(iter(self._hits.values()))
                    return False, 0, max(0.0, oldest_hits[-1] + self.window - now)
                q = deque()
                self._hits[key] = q
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= self.limit:
                retry_after = q[0] + self.window - now
                return False, 0, max(0.0, retry_after)

            q.append(now)
            self._hits.move_to_end(key)
            return True, self.limit - len(q), 0.0


def install_rate_limit(app, limiter: RateLimiter, *, protect_prefix: str = "/v1") -> None:
    """Attach `limiter` to a FastAPI/Starlette app as HTTP middleware.

    Only paths under `protect_prefix` are limited (so /healthz stays open). On
    every response we set the standard `X-RateLimit-*` headers; over-limit
    requests get a 429 with `Retry-After` and an OpenAI-shaped error body.
    """

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        if not request.url.path.startswith(protect_prefix):
            return await call_next(request)

        key = _client_key(request)
        now = time.monotonic()
        allowed, remaining, retry_after = limiter.hit(key, now)

        if not allowed:
            resp = JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": (
                            f"Rate limit exceeded: {limiter.limit} requests per "
                            f"{int(limiter.window)}s. Retry in "
                            f"{retry_after:.1f}s."
                        ),
                        "type": "rate_limit_error",
                    }
                },
            )
            resp.headers["Retry-After"] = str(int(retry_after) + 1)
        else:
            resp = await call_next(request)

        resp.headers["X-RateLimit-Limit"] = str(limiter.limit)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        resp.headers["X-RateLimit-Reset"] = str(int(time.time() + limiter.window))
        return resp


def _client_key(request: Request) -> str:
    """Use the ASGI peer; trusted proxy processing belongs to the ASGI server."""
    return request.client.host if request.client else "unknown"
