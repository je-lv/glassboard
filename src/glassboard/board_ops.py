"""Board-authored advisory events that follow a participant's write.

They run in their own transaction AFTER the participant's operation committed, so that operation
still commits exactly one event; each board operation commits exactly one event of its own,
like the sweeper's `task.stale`. They are advisory: a failure is logged and
never fails the participant's request.

- Loop flag: when the last 10 events of a thread (a reply_to chain) were all written by
  agents or clients, the board posts a `note` in that thread, routed to the coordinators. The
  board's own note breaks the run, so the next flag needs 10 more agent events.
- Storage warning: the first time a session reaches 80% of its event or byte cap, the
  board posts one `note` routed to the coordinators.
"""
from __future__ import annotations

import logging

from . import db, events, ids, inbox, routing

log = logging.getLogger("glassboard")
LOOP_RUN = 10


def _note(c, session_id: str, body: str, data: dict, reply_to: int | None = None) -> int:
    to = events.coordinator_ids(c, session_id)
    recipients, broadcast = routing.route(c, session_id, "note", ids.BOARD_PRINCIPAL_ID, to=to)
    seq, _ = events.append(c, session_id, ids.BOARD_PRINCIPAL_ID, "note", recipients=recipients,
                           broadcast=broadcast, body=body, data=data, reply_to=reply_to)
    return seq


def loop_flag(c, session_id: str, seq: int) -> int | None:
    row = c.execute("SELECT reply_to FROM event WHERE session_id=? AND seq=?", (session_id, seq)).fetchone()
    if row is None or row["reply_to"] is None:
        return None
    root = inbox.thread_root(c, session_id, seq)
    last = inbox.thread_seqs(c, session_id, root)[-LOOP_RUN:]
    if len(last) < LOOP_RUN:
        return None
    q = ",".join("?" * len(last))
    kinds = [r[0] for r in c.execute(
        f"SELECT p.kind FROM event e JOIN principal p ON p.id = e.actor_id WHERE e.session_id=? AND e.seq IN ({q})",
        (session_id, *last))]
    if len(kinds) != LOOP_RUN or any(k not in ("agent", "client") for k in kinds):
        return None
    return _note(c, session_id,
                 f"Possible loop: the last {LOOP_RUN} events in thread #{root} were all written by agents.\n"
                 "A human may want to step in.",
                 {"flag": "loop", "thread": root}, reply_to=seq)


def storage_warning(c, session_id: str) -> int | None:
    sess = c.execute("SELECT head_seq, bytes_used FROM session WHERE id=?", (session_id,)).fetchone()
    if not events.near_cap(sess):
        return None
    if c.execute("SELECT 1 FROM event WHERE session_id=? AND actor_id=? AND type='note' "
                 "AND json_extract(data, '$.flag') = 'storage'", (session_id, ids.BOARD_PRINCIPAL_ID)).fetchone():
        return None
    return _note(c, session_id,
                 f"This session has used 80% of its storage limit ({events.MAX_EVENTS} events or "
                 f"{events.MAX_BYTES // (1024 * 1024)} MiB). New writes are refused at the limit.\n"
                 "Export and archive it, then start a new session.",
                 {"flag": "storage"})


def after_write(database: db.Database, session_id: str, seq: int | None) -> None:
    for op in (lambda c: loop_flag(c, session_id, seq) if seq else None, lambda c: storage_warning(c, session_id)):
        try:
            with database.write() as c:
                op(c)
        except Exception:                      # advisory only; the participant's write is committed
            log.exception("board advisory operation failed session=%s", session_id)
