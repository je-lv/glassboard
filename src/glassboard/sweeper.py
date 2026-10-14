"""The staleness sweeper.

Every interval (default 60 s) the board looks for tasks in `in_progress` or `blocked` whose last
fenced activity is older than their effective threshold (`COALESCE(task.stale_after_s,
session.stale_after_s)`, 300-14400 s) and that were not yet reported for this idle period
(`stale_notified_for != last_activity_at`). For each, one transaction commits one `task.stale` event
authored by `board`, routed to the claimant and the coordinators, and sets
`stale_notified_for = last_activity_at`. Result: one stale event per idle period; the next fenced
operation starts a new period. Staleness is reported, never acted on: state, assignee and fence
stay as they are.

Sessions that are not `active` are skipped: a paused session is expected to be idle, and an
archived one is finished.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from . import db, events, ids, routing

log = logging.getLogger("glassboard")

CANDIDATES = """
SELECT t.session_id, t.num, t.gen, t.claimant_id, t.last_activity_at,
       COALESCE(t.stale_after_s, s.stale_after_s) AS threshold
FROM task t JOIN session s ON s.id = t.session_id
WHERE s.state = 'active' AND t.status IN ('in_progress', 'blocked') AND t.last_activity_at IS NOT NULL
  AND (t.stale_notified_for IS NULL OR t.stale_notified_for != t.last_activity_at)
ORDER BY t.session_id, t.num
"""


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def sweep(database: db.Database, now: datetime | None = None) -> list[tuple[str, int]]:
    """One pass. Returns the (session_id, task num) pairs reported stale. Each report is its own
    transaction, re-checked inside it, so a fenced op that lands meanwhile wins."""
    now = now or datetime.now(timezone.utc)
    with database.read() as c:
        rows = [dict(r) for r in c.execute(CANDIDATES)]
    reported = []
    for r in rows:
        idle = (now - _parse(r["last_activity_at"])).total_seconds()
        if idle <= r["threshold"]:
            continue
        with database.write() as c:
            t = c.execute("SELECT t.*, s.state, COALESCE(t.stale_after_s, s.stale_after_s) AS threshold FROM task t "
                          "JOIN session s ON s.id = t.session_id WHERE t.session_id=? AND t.num=?",
                          (r["session_id"], r["num"])).fetchone()
            if (t is None or t["state"] != "active" or t["status"] not in ("in_progress", "blocked")
                    or t["last_activity_at"] != r["last_activity_at"] or t["stale_notified_for"] == t["last_activity_at"]):
                continue                                    # something changed since the scan
            recipients, broadcast = routing.route(c, t["session_id"], "task.stale", ids.BOARD_PRINCIPAL_ID,
                                                  claimant=t["claimant_id"])
            minutes, limit = int(idle // 60), int(t["threshold"] // 60)
            events.append(c, t["session_id"], ids.BOARD_PRINCIPAL_ID, "task.stale", recipients=recipients,
                          broadcast=broadcast, task_num=t["num"],
                          body=f"No fenced activity for {minutes} min (threshold {limit} min).",
                          data={"gen": t["gen"], "idle_s": int(idle), "threshold_s": t["threshold"],
                                "last_activity_at": t["last_activity_at"]})
            c.execute("UPDATE task SET stale_notified_for=? WHERE session_id=? AND num=?",
                      (t["last_activity_at"], t["session_id"], t["num"]))
            reported.append((t["session_id"], t["num"]))
    return reported


class Sweeper:
    """Runs `sweep` every `interval_s` in a daemon thread for the app's lifetime."""

    def __init__(self, database: db.Database, interval_s: float = 60.0):
        self.database, self.interval = database, interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="glassboard-sweeper", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                sweep(self.database)
            except Exception:                     # never take the server down; next tick retries
                log.exception("stale sweep failed")
            try:
                from .api_coord import expire_leases
                expire_leases(self.database)
            except Exception:
                log.exception("lease expiry failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
