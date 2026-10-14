"""Checkpoints and their inclusion in sync.

A checkpoint is an agent's own handoff note: summary, next action, blockers, tasks, refs, body. Each
save is a new version `n` for (session, principal) and commits one `checkpoint.saved` event (routed
to nobody; the timeline still shows it). `recovery_id` records the recovery period it was saved in, so after a restore the first sync includes it in full.

Reading: yourself, or a coordinator reading anyone's.
Saving a checkpoint, listing tasks in it, or reading it never refreshes task activity.
Fences, consumer tokens and bearer tokens are refused in checkpoint text (protocol rule 10).
"""
from __future__ import annotations

import json

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field, field_validator

from . import db, ids, render, secret_guard, textviews, tokens
from .api_messages import parse_task
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .models import MAX_REFS, Ref, StrictModel
from .mutation import runtime_label, session_tx
from .sessions import require_member, require_state

router = APIRouter(prefix="/api/v1")
MAX_TOTAL = 32 * 1024            # checkpoint total
MAX_SUMMARY = MAX_NEXT = 1024    # per field
SYNC_BODY_LIMIT = 4 * 1024       # body truncated to 4 KiB in sync

class CheckpointIn(StrictModel):
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY)
    next_action: str = Field(min_length=1, max_length=MAX_NEXT)
    blockers: str | None = Field(default=None, min_length=1, max_length=MAX_TOTAL)
    task_nums: list[int | str] = Field(default_factory=list, max_length=50)
    refs: list[Ref] = Field(default_factory=list, max_length=MAX_REFS)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_TOTAL)

    @field_validator("task_nums")
    @classmethod
    def _tasks(cls, v):
        return sorted({parse_task(x) for x in v})


def _size(body: CheckpointIn) -> int:
    parts = [body.summary, body.next_action, body.blockers or "", body.body or "",
             json.dumps([r.model_dump() for r in body.refs])]
    return sum(len(p.encode("utf-8")) for p in parts)


def checkpoint_view(row, handle: str, truncate: int | None = None) -> dict:
    body = row["body"]
    truncated = bool(truncate and body and len(body.encode("utf-8")) > truncate)
    if truncated:
        body = body.encode("utf-8")[:truncate].decode("utf-8", "ignore")
    return {"n": row["n"], "principal": handle, "seq": row["seq"], "saved_at": row["created_at"],
            "recovery_id": row["recovery_id"], "runtime": row["runtime"], "summary": row["summary"],
            "next_action": row["next_action"], "blockers": row["blockers"], "task_nums": json.loads(row["task_nums"]),
            "refs": json.loads(row["refs"]), "last_seen_seq": row["last_seen_seq"], "body": body,
            "truncated": truncated}


def latest(c, session_id: str, principal_id: str):
    return c.execute("SELECT * FROM checkpoint WHERE session_id=? AND principal_id=? ORDER BY n DESC LIMIT 1",
                     (session_id, principal_id)).fetchone()


def sync_block(c, sess, principal_id: str, handle: str, full: bool) -> tuple[int | None, dict | None]:
    """(checkpoint_n, checkpoint) for a sync response. Full when the caller attached or took over,
    when `include_checkpoint` was asked, or when the latest checkpoint was saved under another
    recovery ID (after a restore)."""
    row = latest(c, sess["id"], principal_id)
    if row is None:
        return None, None
    if full or row["recovery_id"] != sess["recovery_id"]:
        return row["n"], checkpoint_view(row, handle, truncate=SYNC_BODY_LIMIT)
    return row["n"], None


@router.post("/sessions/{s}/checkpoints", status_code=201)
def save_checkpoint(s: str, body: CheckpointIn, key: str = Depends(idempotency_key),
                    recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                    runtime: str | None = Depends(runtime_label),
                    p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    if _size(body) > MAX_TOTAL:
        raise ApiError(413, "payload_too_large", "A checkpoint may hold at most 32 KiB in total.")
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        for n in body.task_nums:
            if not c.execute("SELECT 1 FROM task WHERE session_id=? AND num=?", (sid, n)).fetchone():
                raise ApiError(404, "not_found", "A task named in the checkpoint does not exist in this session.")
        tx.check_recovery(recovery)
        # checkpoint fields are stored outside the event, so they are checked here as well.
        secret_guard.check(c, sid, body.summary, body.next_action, body.blockers, body.body,
                           *(x for r in body.refs for x in (r.label, r.loc)))
        payload = {**body.model_dump(), "refs": [r.model_dump() for r in body.refs]}
        replay = tx.replay(key, "POST /sessions/{s}/checkpoints", payload, status=201)
        if replay is not None:
            return replay
        require_state(tx.sess, "checkpoint")
        prev = latest(c, sid, p.id)
        n = (prev["n"] if prev else 0) + 1
        cid, now = ids.new_ulid(), db.utcnow()
        seq, text = tx.event("checkpoint.saved", data={"n": n, "summary": body.summary},
                             receipt=lambda seq: {"session": tx.sess["handle"], "n": n, "seq": seq})
        c.execute("INSERT INTO checkpoint(session_id,principal_id,n,id,seq,recovery_id,runtime,summary,next_action,"
                  "blockers,task_nums,refs,last_seen_seq,body,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, p.id, n, cid, seq, tx.sess["recovery_id"], runtime, body.summary, body.next_action,
                   body.blockers, json.dumps(body.task_nums), json.dumps([r.model_dump(exclude_none=True) for r in body.refs]),
                   tx.me["cursor_seq"], body.body, now))
    return respond(text, 201)


@router.get("/sessions/{s}/checkpoints")
def get_checkpoint(s: str, request: Request, principal: str = Query("me", max_length=80),
                   n: str = Query("latest", max_length=12), format: Literal["json", "text"] = Query("json"),
                   p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, me = require_member(c, s, p)
        if principal in ("me", p.handle):
            pid, handle = p.id, p.handle
        else:
            if me["role"] != "coordinator":
                raise ApiError(403, "forbidden", "Only coordinators read other members' checkpoints.")
            row = c.execute("SELECT id, handle FROM principal WHERE handle=?", (principal,)).fetchone()
            if row is None:
                raise ApiError(404, "not_found", "No such principal.")
            pid, handle = row["id"], row["handle"]
        if n == "latest":
            row = latest(c, sess["id"], pid)
        elif n.isdigit():
            row = c.execute("SELECT * FROM checkpoint WHERE session_id=? AND principal_id=? AND n=?",
                            (sess["id"], pid, int(n))).fetchone()
        else:
            raise ApiError(422, "invalid_request", "The request failed validation.",
                           details={"fields": [{"loc": ["query", "n"], "type": "value_error"}]})
        if row is None:
            raise ApiError(404, "not_found", "No such checkpoint.")
        view = checkpoint_view(row, handle)
    if format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain"):
        return PlainTextResponse(textviews.checkpoint_text(view))
    return {"session": sess["handle"], "checkpoint": view}
