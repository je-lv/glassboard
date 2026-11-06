"""Quiet hours (B56, ERRATA E38): `POST /sessions/{s}/quiet-hours`.

Only humans set them: an agent's owner or a human coordinator for an agent, a human for itself.
Setting, changing or clearing a window is one `member.quiet` event, routed to the member. During the
window the board dispatches no wake to that agent (unless `urgent` and a human caused the demand)
and the dashboard sends that human no browser notification. Nothing is hidden or dropped; the
attention queue, messages, sync and history are unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import Field, StrictBool

from . import db, quiet, tokens
from .deps import get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import PRINCIPAL_HANDLE_RE, require_state

router = APIRouter(prefix="/api/v1")


class QuietIn(StrictModel):
    member: str = Field(pattern=PRINCIPAL_HANDLE_RE.pattern)
    start: str | None = Field(default=None, max_length=5)        # HH:MM; all of start, end, tz, or none (clear)
    end: str | None = Field(default=None, max_length=5)
    tz: str | None = Field(default=None, max_length=64)
    urgent: StrictBool = False


def _bad(field: str) -> ApiError:
    return ApiError(422, "invalid_request", "The request failed validation.",
                    hint="start and end are HH:MM (24-hour) and differ; tz is an IANA name such as Europe/Madrid.",
                    details={"fields": [{"loc": ["body", field], "type": "invalid_value"}]})


@router.post("/sessions/{s}/quiet-hours")
def set_quiet(s: str, body: QuietIn, key: str = Depends(idempotency_key),
              recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
              runtime: str | None = Depends(runtime_label),
              p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Set or clear one member's daily quiet window (humans only)."""
    given = [body.start, body.end, body.tz]
    if any(v is not None for v in given) and not all(v is not None for v in given):
        raise _bad("start" if body.start is None else "end" if body.end is None else "tz")
    if body.start is not None:
        for field, check in (("start", lambda: quiet.parse_hhmm(body.start)), ("end", lambda: quiet.parse_hhmm(body.end)),
                             ("tz", lambda: quiet.zone(body.tz)), ("end", lambda: quiet.validate(body.start, body.end, body.tz))):
            try:
                check()
            except ValueError:
                raise _bad(field) from None
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        tx.check_recovery(recovery)
        target = c.execute("SELECT p.id, p.handle, p.kind, p.owner_id FROM principal p JOIN member m ON m.principal_id=p.id "
                           "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (body.member, sid)).fetchone()
        if target is None:
            raise ApiError(404, "not_found", "No such member in this session.")
        replay = tx.replay(key, "POST /sessions/{s}/quiet-hours", {**body.model_dump(), "member": target["id"]})
        if replay is not None:
            return replay
        human_coord = p.kind == "human" and tx.me["role"] == "coordinator"
        allowed = p.kind == "human" and (
            (target["kind"] == "human" and target["id"] == p.id)
            or (target["kind"] in ("agent", "client") and (target["owner_id"] == p.id or human_coord)))
        if not allowed:
            raise ApiError(403, "forbidden", "Only humans set quiet hours: for themselves, or for an agent they own "
                                             "or coordinate.")
        require_state(tx.sess, "message")
        cleared = body.start is None
        c.execute("UPDATE member SET quiet_start=?, quiet_end=?, quiet_tz=?, quiet_urgent=? WHERE session_id=? "
                  "AND principal_id=?", (body.start, body.end, body.tz, int(body.urgent and not cleared), sid, target["id"]))
        window = "off" if cleared else f"{body.start}-{body.end} {body.tz}"
        data = {"handle": target["handle"], "quiet": window}
        if not cleared:
            data["urgent"] = "human-caused wakes still go through" if body.urgent else "no exceptions"
        _, text = tx.event("member.quiet", data=data, member=target["id"],
                           receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "member": target["handle"],
                                                "quiet": window, "urgent": bool(body.urgent and not cleared)})
    return respond(text, 200)
