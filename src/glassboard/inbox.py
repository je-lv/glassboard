"""Reading the log: event views, the inbox, owed questions.

Inbox of principal P in session S: events with seq > cursor, not
authored by P, and either routed to P (an event_recipient row) or broadcast with seq > P's
joined_seq.

Obligations: a `question` routed to recipients R creates one obligation per recipient;
broadcast questions create none. The obligation of r is resolved by r's `answer` with re = the
question, by r's own `question.resolved` (scope self), or by any `question.resolved` with scope
all (asker or coordinator). Acks never resolve; resolving never acks.

Every JSON view of an event goes through `event_view`, which never includes `receipt`,
`req_digest` or `op_id` (receipts can hold fences and consumer tokens).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import render  # noqa: F401  (kept for callers)
from .textviews import view_lines  # noqa: F401  re-exported

INBOX_SQL = """
SELECT e.* FROM event e
WHERE e.session_id = :s AND e.seq > :cursor AND e.seq <= :head AND e.actor_id != :p
  AND (EXISTS (SELECT 1 FROM event_recipient r WHERE r.session_id = e.session_id AND r.seq = e.seq
                                                AND r.principal_id = :p)
       OR (e.broadcast = 1 AND e.seq > :joined))
ORDER BY e.seq LIMIT :limit
"""

OWED_SQL = """
SELECT q.seq, q.ts, q.actor_id, q.type FROM event q
JOIN event_recipient er ON er.session_id = q.session_id AND er.seq = q.seq AND er.principal_id = :p
WHERE q.session_id = :s AND q.type IN ('question', 'approval')
  AND NOT EXISTS (SELECT 1 FROM event a WHERE a.session_id = q.session_id AND a.reply_to = q.seq
                  AND a.type = 'answer' AND a.actor_id = :p)
  AND NOT EXISTS (SELECT 1 FROM event d WHERE d.session_id = q.session_id AND d.reply_to = q.seq
                  AND d.type IN ('approval.granted', 'approval.denied'))
  AND NOT EXISTS (SELECT 1 FROM event r WHERE r.session_id = q.session_id AND r.reply_to = q.seq
                  AND r.type = 'question.resolved'
                  AND (json_extract(r.data, '$.scope') = 'all' OR r.actor_id = :p))
ORDER BY q.seq
"""


def handle_map(conn: sqlite3.Connection, ids) -> dict[str, str]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(f"SELECT id, handle FROM principal WHERE id IN ({q})", ids)}


def recipients(conn: sqlite3.Connection, session_id: str, seq: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT p.handle FROM event_recipient er JOIN principal p ON p.id = er.principal_id "
        "WHERE er.session_id=? AND er.seq=? ORDER BY p.handle", (session_id, seq))]


def event_view(conn: sqlite3.Connection, row, handles: dict[str, str] | None = None) -> dict:
    """The public JSON form of one event. Never receipts, digests or op IDs."""
    handles = handles if handles is not None else handle_map(conn, [row["actor_id"]])
    data = json.loads(row["data"]) if row["data"] else None
    refs = data.pop("refs", None) if isinstance(data, dict) else None
    return {"seq": row["seq"], "ts": row["ts"], "actor": handles.get(row["actor_id"], "?"), "runtime": row["runtime"],
            "type": row["type"], "to": "all" if row["broadcast"] else recipients(conn, row["session_id"], row["seq"]),
            "task": row["task_num"], "re": row["reply_to"], "body": row["body"], "data": data or None,
            "refs": refs or [], "epoch": row["epoch"]}


def views(conn: sqlite3.Connection, rows) -> list[dict]:
    rows = list(rows)
    h = handle_map(conn, [r["actor_id"] for r in rows])
    return [event_view(conn, r, h) for r in rows]


def inbox_page(conn: sqlite3.Connection, sess, me, cursor: int, limit: int) -> tuple[list, bool]:
    rows = conn.execute(INBOX_SQL, {"s": sess["id"], "cursor": cursor, "head": sess["head_seq"], "p": me["principal_id"],
                                    "joined": me["joined_seq"], "limit": limit + 1}).fetchall()
    return rows[:limit], len(rows) > limit


def _age_s(ts: str) -> int:
    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


def owed(conn: sqlite3.Connection, session_id: str, principal_id: str, show: int = 10) -> tuple[list[dict], int]:
    """My unresolved obligations, oldest first: at most `show` listed, the total exact."""
    rows = conn.execute(OWED_SQL, {"s": session_id, "p": principal_id}).fetchall()
    h = handle_map(conn, [r["actor_id"] for r in rows[:show]])
    return [{"seq": r["seq"], "from": h.get(r["actor_id"], "?"), "age_s": _age_s(r["ts"]), "type": r["type"]}
            for r in rows[:show]], len(rows)


def obligation_holders(conn: sqlite3.Connection, session_id: str, q_seq: int) -> list[str]:
    """Recipients of question q_seq whose obligation is still open."""
    return [r[0] for r in conn.execute("""
        SELECT er.principal_id FROM event_recipient er
        WHERE er.session_id = :s AND er.seq = :q
          AND NOT EXISTS (SELECT 1 FROM event a WHERE a.session_id = :s AND a.reply_to = :q
                          AND a.type = 'answer' AND a.actor_id = er.principal_id)
          AND NOT EXISTS (SELECT 1 FROM event d WHERE d.session_id = :s AND d.reply_to = :q
                          AND d.type IN ('approval.granted', 'approval.denied'))
          AND NOT EXISTS (SELECT 1 FROM event r WHERE r.session_id = :s AND r.reply_to = :q
                          AND r.type = 'question.resolved'
                          AND (json_extract(r.data, '$.scope') = 'all' OR r.actor_id = er.principal_id))
        ORDER BY er.principal_id""", {"s": session_id, "q": q_seq})]


def thread_seqs(conn: sqlite3.Connection, session_id: str, root: int) -> list[int]:
    """`root` and every event whose reply_to chain leads to it, in seq order."""
    rows = conn.execute("""
        WITH RECURSIVE t(seq) AS (
          SELECT seq FROM event WHERE session_id = :s AND seq = :root
          UNION SELECT e.seq FROM event e JOIN t ON e.session_id = :s AND e.reply_to = t.seq)
        SELECT seq FROM t ORDER BY seq""", {"s": session_id, "root": root}).fetchall()
    return [r[0] for r in rows]


def thread_root(conn: sqlite3.Connection, session_id: str, seq: int) -> int:
    seen = 0
    while seen < 10_000:
        r = conn.execute("SELECT reply_to FROM event WHERE session_id=? AND seq=?", (session_id, seq)).fetchone()
        if r is None or r[0] is None:
            return seq
        seq, seen = r[0], seen + 1
    return seq
