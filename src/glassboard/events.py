"""Appending to a session's event log.

`append()` must run inside the caller's write transaction (`Database.write()`), together with the
side effects it describes: exactly one event per coordination mutation. It allocates
`seq = head_seq + 1` (gapless; BEGIN IMMEDIATE serialises allocation), stamps the session's
current `recovery_id` and `epoch`, stores the receipt (if any), and writes one `event_recipient`
row per routed principal. The event row is immutable afterwards (append-only triggers).
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Callable, Iterable

from . import db, ids

# Every event type. Appending any other type is a bug.
TYPES = frozenset({
    "question", "answer", "update", "proposal", "blocker", "note", "decision", "question.resolved",
    "message.preflight", "artifact.added",
    "task.created", "task.edited", "task.acknowledged", "task.claimed", "task.note", "task.checked", "task.reviewed", "lease.taken", "lease.released", "lease.expired", "approval", "approval.granted", "approval.denied", "task.blocked", "task.unblocked",
    "task.submitted", "task.released", "task.reclaimed", "task.accepted", "task.returned",
    "task.reassigned", "task.cancelled", "task.reopened", "task.stale",
    "checkpoint.saved", "consumer.attached", "consumer.takeover",
    "member.added", "member.removed", "member.changed", "member.jobrole",  # changed: permission role change
    "session.created", "session.paused", "session.resumed", "session.archived",
    "system.restored",
})
# Types appear inside header lines, whose grammar allows only [a-z.].
assert all(re.fullmatch(r"[a-z.]+", t) for t in TYPES), "event types must match the header grammar"


def append(conn: sqlite3.Connection, session_id: str, actor_id: str, type_: str, *,
           recipients: Iterable[str] = (), broadcast: bool = False, runtime: str | None = None,
           task_num: int | None = None, reply_to: int | None = None, body: str | None = None,
           data: dict | None = None, op_id: str | None = None, req_digest: bytes | None = None,
           receipt: Callable[[int], str] | None = None) -> tuple[int, str | None]:
    """Append one event; returns (seq, receipt_text). `receipt` receives the new seq and returns
    the exact response body, so the receipt can name its own event."""
    if type_ not in TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    s = conn.execute("SELECT head_seq, recovery_id, epoch FROM session WHERE id=?", (session_id,)).fetchone()
    if s is None:
        raise LookupError("session vanished inside its own transaction")
    seq = s["head_seq"] + 1
    now = db.utcnow()
    text = receipt(seq) if receipt else None
    data_text = json.dumps(data, sort_keys=True) if data is not None else None
    size = len((body or "").encode("utf-8")) + len((data_text or "").encode("utf-8"))
    conn.execute("UPDATE session SET head_seq=?, updated_at=?, bytes_used=bytes_used+? WHERE id=?",
                 (seq, now, size, session_id))
    conn.execute(
        "INSERT INTO event(session_id,seq,id,recovery_id,epoch,ts,actor_id,runtime,type,broadcast,"
        "task_num,reply_to,body,data,op_id,req_digest,receipt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, seq, ids.new_ulid(), s["recovery_id"], s["epoch"], now, actor_id, runtime, type_,
         int(broadcast), task_num, reply_to, body, data_text, op_id, req_digest, text))
    rset = frozenset(recipients)
    for pid in sorted(rset):
        conn.execute("INSERT INTO event_recipient(session_id,seq,principal_id) VALUES (?,?,?)",
                     (session_id, seq, pid))
    database = db.current_database()
    if database is not None and database.event_listeners:
        # After COMMIT only (a rollback drops it): wake long-poll waiters.
        listeners = list(database.event_listeners)
        db.on_commit(lambda: [fn(session_id, seq, type_, actor_id, rset, broadcast) for fn in listeners])
    return seq, text


# Per-session storage cap; participant writes beyond it are refused (413).
MAX_EVENTS = 50_000
MAX_BYTES = 200 * 1024 * 1024
WARN_FRACTION = 0.8


def over_cap(sess) -> bool:
    return sess["head_seq"] >= MAX_EVENTS or sess["bytes_used"] >= MAX_BYTES


def near_cap(sess) -> bool:
    return sess["head_seq"] >= WARN_FRACTION * MAX_EVENTS or sess["bytes_used"] >= WARN_FRACTION * MAX_BYTES


def coordinator_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT principal_id FROM member WHERE session_id=? AND role='coordinator' AND left_at IS NULL",
        (session_id,))]
