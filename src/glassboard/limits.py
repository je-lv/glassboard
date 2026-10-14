"""Request limits that live in memory (no events, no rows).

AuthFailureLimiter (10 auth failures/min/IP → 429):
- every 401 from bearer authentication counts as one failure for the client address;
- while an address has 10 or more failures in the last 60 s, every authenticated request from it
  gets 429 `rate_limited` with Retry-After, BEFORE the token is checked, so guessing stops even
  for a correct guess;
- the client address is the TCP peer as seen by the app; behind Caddy that is only correct when
  uvicorn runs with `--proxy-headers --forwarded-allow-ips 127.0.0.1`.
State is per process and lost on restart (acceptable: one worker). The table is bounded.

BodyLimitMiddleware: counts the bytes the server actually receives, so a chunked
body without Content-Length cannot bypass the limit; Content-Length above the limit is refused
before reading anything.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import deque


class AuthFailureLimiter:
    def __init__(self, limit: int = 10, window_s: float = 60.0, max_addresses: int = 10_000,
                 clock=time.monotonic):
        self.limit, self.window, self.max_addresses, self.clock = limit, window_s, max_addresses, clock
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] >= self.window:
            q.popleft()

    def retry_after(self, addr: str) -> int | None:
        """Seconds until `addr` may try again, or None if it is not limited."""
        with self._lock:
            q = self._fails.get(addr)
            if not q:
                return None
            now = self.clock()
            self._prune(q, now)
            if len(q) < self.limit:
                if not q:
                    del self._fails[addr]
                return None
            # Allowed again when enough old failures leave the window.
            return max(1, math.ceil(q[len(q) - self.limit] + self.window - now))

    def record_failure(self, addr: str) -> None:
        with self._lock:
            now = self.clock()
            if addr not in self._fails and len(self._fails) >= self.max_addresses:
                self._evict(now)
            q = self._fails.setdefault(addr, deque())
            self._prune(q, now)
            q.append(now)

    def _evict(self, now: float) -> None:
        for a in list(self._fails):
            self._prune(self._fails[a], now)
            if not self._fails[a]:
                del self._fails[a]
        if len(self._fails) >= self.max_addresses:     # still full: drop the stalest address
            stalest = min(self._fails, key=lambda a: self._fails[a][-1])
            del self._fails[stalest]


def _envelope_413(request_id: str) -> bytes:
    return json.dumps({"error": {"code": "payload_too_large", "message": "Payload too large.",
                                 "request_id": request_id, "retryable": False}}).encode()


class BodyLimitMiddleware:
    """Pure ASGI. Buffers at most `max_body` bytes, then replays them to the app."""

    def __init__(self, app, max_body: int, artifact_max_body: int | None = None):
        self.app, self.max_body, self.artifact_max_body = app, max_body, artifact_max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        path = scope.get("path", "")
        artifact_upload = (scope.get("method") == "POST" and
                           (re.fullmatch(r"/api/v1/sessions/[^/]+/artifacts", path) is not None or
                            re.fullmatch(r"/s/[^/]+/artifacts/upload", path) is not None))
        max_body = self.artifact_max_body if artifact_upload and self.artifact_max_body else self.max_body
        cl = headers.get(b"content-length")
        too_big = cl is not None and (not cl.isdigit() or int(cl) > max_body)
        chunks, total = [], 0
        if not too_big:
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body = msg.get("body", b"")
                total += len(body)
                if total > max_body:
                    too_big = True
                    break
                chunks.append(body)
                if not msg.get("more_body", False):
                    break
        if too_big:
            state = scope.setdefault("state", {})
            rid = state.get("request_id") or "req_" + uuid.uuid4().hex[:16]
            state["request_id"] = rid
            body = _envelope_413(rid)
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"x-request-id", rid.encode()), (b"cache-control", b"no-store"),
                                    (b"x-content-type-options", b"nosniff"), (b"connection", b"close")]})
            await send({"type": "http.response.body", "body": body})
            return
        data = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()      # after the body: pass through (e.g. http.disconnect)

        return await self.app(scope, replay, send)


class PostRateLimiter:
    """Agent posts: 30/min with bursts of 10, a token bucket per principal.
    Humans are not limited (their posts are logged like everything else). In memory."""

    def __init__(self, per_minute: float = 30, burst: int = 10, clock=time.monotonic):
        self.rate, self.burst, self.clock = per_minute / 60.0, burst, clock
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, principal_id: str) -> int | None:
        """Consume one post. Returns None if allowed, else the seconds to wait."""
        with self._lock:
            now = self.clock()
            tokens, last = self._b.get(principal_id, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._b[principal_id] = (tokens - 1, now)
                return None
            self._b[principal_id] = (tokens, now)
            return max(1, math.ceil((1 - tokens) / self.rate))
