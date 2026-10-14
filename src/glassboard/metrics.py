"""Prometheus metrics at GET /metrics, admin-only.

In-process counters and histograms (one worker), plus values read from the database at scrape
time, so the persistent ones (events per type, takeovers) survive restarts. Label values are only
server-chosen identifiers (route templates, error codes, event types): never client text.
"""
from __future__ import annotations

import bisect
import threading

# Metric names, from the error code that marks each condition.
ERROR_COUNTERS = {
    "forbidden": "authz_denied_total",
    "recovery_mismatch": "recovery_mismatch_total",
    "ack_beyond_delivered": "ack_beyond_delivered_total",
    "fence_mismatch": "fence_mismatch_total",
    "task_already_claimed": "claim_conflict_total",
    "rate_limited": "rate_limited_total",
}
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
BYTES_BUCKETS = (256, 1024, 4096, 16384, 65536, 262144, 1048576)


class Histogram:
    def __init__(self, buckets):
        self.buckets, self.counts, self.sum, self.n = buckets, [0] * (len(buckets) + 1), 0.0, 0

    def observe(self, v: float) -> None:
        self.counts[bisect.bisect_left(self.buckets, v)] += 1
        self.sum += v
        self.n += 1

    def lines(self, name: str, labels: str) -> list[str]:
        out, cum = [], 0
        sep = "," if labels else ""
        for b, c in zip(self.buckets, self.counts):
            cum += c
            out.append(f'{name}_bucket{{{labels}{sep}le="{b}"}} {cum}')
        out.append(f'{name}_bucket{{{labels}{sep}le="+Inf"}} {self.n}')
        out.append(f"{name}_sum{{{labels}}} {self.sum:.6f}" if labels else f"{name}_sum {self.sum:.6f}")
        out.append(f"{name}_count{{{labels}}} {self.n}" if labels else f"{name}_count {self.n}")
        return out


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[str, int] = {name: 0 for name in set(ERROR_COUNTERS.values())}
        self.counters.update(replay_total=0, sqlite_busy_total=0)
        self.latency: dict[tuple[str, str], Histogram] = {}
        self.sync_bytes = Histogram(BYTES_BUCKETS)

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def error(self, code: str) -> None:
        if code in ERROR_COUNTERS:
            self.inc(ERROR_COUNTERS[code])

    def request(self, method: str, route: str, seconds: float) -> None:
        with self._lock:
            self.latency.setdefault((method, route), Histogram(LATENCY_BUCKETS)).observe(seconds)

    def sync_response(self, nbytes: int) -> None:
        with self._lock:
            self.sync_bytes.observe(nbytes)

    def render(self, conn) -> str:
        lines = []
        with self._lock:
            for name in sorted(self.counters):
                lines += [f"# TYPE glassboard_{name} counter", f"glassboard_{name} {self.counters[name]}"]
            lines.append("# TYPE glassboard_request_duration_seconds histogram")
            for (method, route), h in sorted(self.latency.items()):
                lines += h.lines("glassboard_request_duration_seconds", f'method="{method}",route="{route}"')
            lines.append("# TYPE glassboard_sync_bytes histogram")
            lines += self.sync_bytes.lines("glassboard_sync_bytes", "")
        lines.append("# TYPE glassboard_events_total counter")
        for t, n in conn.execute("SELECT type, count(*) FROM event GROUP BY type ORDER BY type"):
            lines.append(f'glassboard_events_total{{type="{t}"}} {n}')
        takeovers = conn.execute("SELECT count(*) FROM event WHERE type='consumer.takeover'").fetchone()[0]
        lines += ["# TYPE glassboard_consumer_takeover_total counter", f"glassboard_consumer_takeover_total {takeovers}"]
        stale = conn.execute(
            "SELECT count(*) FROM task t JOIN session s ON s.id = t.session_id WHERE t.status IN ('in_progress','blocked') "
            "AND t.last_activity_at IS NOT NULL AND (julianday('now') - julianday(t.last_activity_at)) * 86400 "
            "> COALESCE(t.stale_after_s, s.stale_after_s)").fetchone()[0]
        lines += ["# TYPE glassboard_stale_tasks gauge", f"glassboard_stale_tasks {stale}"]
        return "\n".join(lines) + "\n"
