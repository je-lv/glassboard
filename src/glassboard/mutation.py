"""The session-mutation pipeline: check order and idempotent replay.

Every session mutation runs inside one `SessionTx`:

    with session_tx(database, p, s) as tx:            # BEGIN IMMEDIATE; membership → 404
        target = ...                                   # referenced objects → 404
        tx.check_recovery(recovery_header)             # current recovery ID required → 428 / 409
        if (r := tx.replay(key, operation, payload, target=..., runtime=...)) is not None:
            return r                                   # same key + same digest → stored receipt
        ...role / kind (403), consumer / fence / state (409)...
        seq, text = tx.event(type_, receipt=lambda seq: {...}, **routing)
    return respond(text, status)

Replay is checked AFTER the recovery check: an op from an earlier recovery period gets
`recovery_mismatch` and is never matched against receipts. The digest excludes the recovery ID,
so the same op resent under the current recovery ID replays its receipt if it committed before
(`glassboard reconcile --resend`). The key scope is UNIQUE(session_id, actor_id, op_id).
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Callable, Iterator

from fastapi import Header
from fastapi.responses import Response

from . import db, events, idempotency, routing, secret_guard, sessions, tokens
from .errors import ApiError

RUNTIME_RE = re.compile(r"^[a-z0-9-]{1,16}$")


def runtime_label(x_glassboard_runtime: str | None = Header(None, alias="X-Glassboard-Runtime")) -> str | None:
    """The runtime label is structural; invalid → 422, never sanitised and stored."""
    if x_glassboard_runtime is None:
        return None
    if not RUNTIME_RE.match(x_glassboard_runtime):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       details={"fields": [{"loc": ["header", "x-glassboard-runtime"], "type": "string_pattern_mismatch"}]})
    return x_glassboard_runtime


class SessionTx:
    def __init__(self, conn, sess, me, p: tokens.Principal, runtime: str | None):
        self.conn, self.sess, self.me, self.p, self.runtime = conn, sess, me, p, runtime
        self.key: str | None = None
        self.digest: bytes | None = None

    @property
    def session_id(self) -> str:
        return self.sess["id"]

    def check_recovery(self, presented: str | None) -> None:
        sessions.check_recovery(self.sess, presented)

    def replay(self, key: str, operation: str, payload: dict, *, target: dict | None = None,
               bind_runtime: bool = False, status: int = 200) -> Response | None:
        """Request digest and the receipt lookup. Remembers key + digest for `event()`."""
        self.key = key
        self.digest = idempotency.digest(operation, self.p.id, payload, session_id=self.session_id,
                                         target=target, runtime=self.runtime if bind_runtime else None)
        row = self.conn.execute("SELECT req_digest, receipt FROM event WHERE session_id=? AND actor_id=? AND op_id=?",
                                (self.session_id, self.p.id, key)).fetchone()
        return idempotency.check_replay(row, self.digest, status)

    def event(self, type_: str, *, receipt: Callable[[int], dict] | None = None, body: str | None = None,
              data: dict | None = None, task_num: int | None = None, reply_to: int | None = None,
              capped: bool = True, **route_args) -> tuple[int, str | None]:
        """`capped=False` only for operations that must work on a full session (consumer
        attach/takeover: reading and acking must never be blocked by the storage cap)."""
        # no live secret and nothing shaped like a bearer token enters the log.
        secret_guard.check(self.conn, self.session_id, body, *secret_guard.strings(data or {}))
        if capped:
            fresh = self.conn.execute("SELECT head_seq, bytes_used FROM session WHERE id=?", (self.session_id,)).fetchone()
            if events.over_cap(fresh):
                raise ApiError(413, "payload_too_large", "This session has reached its storage limit.",
                               hint="A coordinator can export and archive it, then start a new session.")
        recipients, broadcast = routing.route(self.conn, self.session_id, type_, self.p.id, **route_args)
        build = (lambda seq: idempotency.receipt_body(receipt(seq))) if receipt else None
        return events.append(self.conn, self.session_id, self.p.id, type_, recipients=recipients,
                             broadcast=broadcast, runtime=self.runtime, task_num=task_num, reply_to=reply_to,
                             body=body, data=data, op_id=self.key, req_digest=self.digest, receipt=build)


@contextmanager
def session_tx(database: db.Database, p: tokens.Principal, s: str, runtime: str | None = None) -> Iterator[SessionTx]:
    with database.write() as conn:
        sess, me = sessions.require_member(conn, s, p)
        yield SessionTx(conn, sess, me, p, runtime)
