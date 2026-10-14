"""Long-poll waits for POST /sync (`wait`).

A sync whose page is empty may wait up to `wait` seconds (≤ 25) for something to arrive. The wait
holds no lock and no transaction: the request awaits an asyncio event that is set, after
COMMIT, by any event that would enter the caller's inbox (routed to it, or a broadcast it did not
write), or by a change to its own consumer (attach/takeover), so a superseded runtime learns at once.
The waiter is registered BEFORE the first transaction, so a commit between that transaction and the
wait cannot be missed. After waking, the sync runs a second transaction that re-validates the
consumer and the recovery ID and recomputes the page; it never applies the ack twice.
Caps: at most 2 concurrent waits per principal and 50 per instance; beyond that,
`429 rate_limited`. Waiters may live on different event loops (tests); each is woken on its own.
"""
from __future__ import annotations

import asyncio
import threading

from .errors import ApiError

PER_PRINCIPAL, PER_INSTANCE = 2, 50
MAX_WAIT_S = 25


class Waiter:
    __slots__ = ("session_id", "principal_id", "event", "loop")

    def __init__(self, session_id: str, principal_id: str):
        self.session_id, self.principal_id = session_id, principal_id
        self.event = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def wake(self) -> None:
        self.loop.call_soon_threadsafe(self.event.set)


class Waiters:
    def __init__(self, per_principal: int = PER_PRINCIPAL, per_instance: int = PER_INSTANCE):
        self.per_principal, self.per_instance = per_principal, per_instance
        self._lock = threading.Lock()
        self._all: set[Waiter] = set()

    def register(self, session_id: str, principal_id: str) -> Waiter:
        with self._lock:
            mine = sum(1 for w in self._all if w.principal_id == principal_id)
            if mine >= self.per_principal or len(self._all) >= self.per_instance:
                raise ApiError(429, "rate_limited", "Too many waiting syncs.", retryable=True,
                               hint="At most 2 waiting syncs per principal; retry without waiting or later.",
                               headers={"Retry-After": "1"})
            w = Waiter(session_id, principal_id)
            self._all.add(w)
            return w

    def release(self, w: Waiter) -> None:
        with self._lock:
            self._all.discard(w)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._all)

    def on_event(self, session_id, seq, type_, actor_id, recipients, broadcast) -> None:
        """Database.event_listeners hook, called after COMMIT (any thread)."""
        with self._lock:
            targets = [w for w in self._all if w.session_id == session_id]
        for w in targets:
            mine = w.principal_id == actor_id
            if (not mine and (broadcast or w.principal_id in recipients)) or (mine and type_.startswith("consumer.")):
                w.wake()
