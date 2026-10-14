"""Messages, question resolution, history and export.

Check order inside one transaction: membership (404) → referenced objects: `re`, `task`
(404), recipients (422 `recipient_not_member`) → recovery (428/409) → replay → role (403:
observers may not post; `decision` is human-coordinator only) → agent post rate (429) →
storage cap (413) → one event. History and export are read-only: no recovery header, no
consumer, and they never move a cursor.
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import Field, StrictInt, ValidationError, field_validator

from . import board_ops, db, inbox, render, secret_guard, tokens
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError, safe_fields
from .idempotency import idempotency_key, respond
from .models import MAX_REFS, Ref, StrictModel
from .mutation import runtime_label, session_tx
from .sessions import PRINCIPAL_HANDLE_RE, require_member, require_state

router = APIRouter(prefix="/api/v1")
MAX_BODY_BYTES = 16 * 1024          # message body limit
MAX_RECIPIENTS = 20
KINDS = ("question", "answer", "update", "proposal", "blocker", "note", "decision", "approval")
TASK_RE = re.compile(r"^T?([1-9][0-9]{0,8})$")


def parse_task(v) -> int:
    """`{t}` accepts `7` or `T7`. Structural: anything else is rejected."""
    m = TASK_RE.match(str(v)) if isinstance(v, (int, str)) and not isinstance(v, bool) else None
    if not m:
        raise ValueError("task must look like 7 or T7")
    return int(m.group(1))


class Draft(StrictModel):
    """A proposed task inside a `proposal` message: a breakdown a human can turn into tasks."""
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=2000)
    deliverable: str = Field(min_length=1, max_length=512)
    assignee: str | None = Field(default=None, pattern=PRINCIPAL_HANDLE_RE.pattern)


MAX_DRAFTS = 10


class Standup(StrictModel):
    """Structured stand-up fields of an `update`: what got done, what is in progress, what blocks."""
    done: str | None = Field(default=None, min_length=1, max_length=1024)
    doing: str | None = Field(default=None, min_length=1, max_length=1024)
    blocked: str | None = Field(default=None, min_length=1, max_length=1024)


class MessageIn(StrictModel):
    kind: Literal["question", "answer", "update", "proposal", "blocker", "note", "decision", "approval"]
    body: str = Field(min_length=1, max_length=MAX_BODY_BYTES)
    # Not pattern-validated here: an unknown or malformed handle is reported back as data
    # (422 recipient_not_member, details.handles), never in the message text.
    to: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    re: StrictInt | None = Field(default=None, ge=1)
    task: int | str | None = None
    refs: list[Ref] = Field(default_factory=list, max_length=MAX_REFS)
    drafts: list[Draft] = Field(default_factory=list, max_length=MAX_DRAFTS)
    standup: Standup | None = None
    preflight_seq: StrictInt | None = Field(default=None, ge=1)

    @field_validator("body")
    @classmethod
    def _body_bytes(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("body is larger than 16 KiB")
        return v

    @field_validator("to")
    @classmethod
    def _to_items(cls, v: list[str]) -> list[str]:
        if any(len(h) > 80 for h in v):
            raise ValueError("recipient handle too long")
        return v

    @field_validator("task")
    @classmethod
    def _task(cls, v):
        return None if v is None else parse_task(v)


def _wants_text(request: Request, format: str | None) -> bool:
    return format == "text" or request.headers.get("accept", "").split(",")[0].strip().startswith("text/plain")


@router.post("/sessions/{s}/messages", status_code=201)
def post_message(s: str, body: MessageIn, request: Request, key: str = Depends(idempotency_key),
                 recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                 runtime: str | None = Depends(runtime_label),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return send_message(database, request.app.state.post_limiter, p, s, body, key, recovery, runtime)


def _message_digest(body: MessageIn) -> str:
    """Bind a preflight to the exact validated message, excluding its link field."""
    value = body.model_dump(exclude={"preflight_seq"}, exclude_none=True)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _validate_message(c, sid: str, p: tokens.Principal, role: str, body: MessageIn,
                      *, semantic: bool = True) -> list[str]:
    """Checks shared by preflight and send. Return recipient IDs; neither path sends here."""
    if body.re is not None and not c.execute("SELECT 1 FROM event WHERE session_id=? AND seq=?", (sid, body.re)).fetchone():
        raise ApiError(404, "not_found", "The event named by `re` does not exist in this session.")
    if body.task is not None and not c.execute("SELECT 1 FROM task WHERE session_id=? AND num=?", (sid, body.task)).fetchone():
        raise ApiError(404, "not_found", "The task named by `task` does not exist in this session.")
    to_ids, bad = [], []
    for h in dict.fromkeys(body.to):
        row = None
        if PRINCIPAL_HANDLE_RE.match(h):
            row = c.execute("SELECT p.id FROM principal p JOIN member m ON m.principal_id = p.id "
                            "WHERE p.handle=? AND m.session_id=? AND m.left_at IS NULL", (h, sid)).fetchone()
        (to_ids.append(row["id"]) if row else bad.append(h))
    if bad:
        raise ApiError(422, "recipient_not_member", "Some recipients are not members of this session.",
                       hint="Check the handles with `glassboard members list`.", details={"handles": bad})
    if not semantic:
        return to_ids
    if role == "observer":
        raise ApiError(403, "forbidden", "Observers may read but not post.")
    if body.standup is not None and (body.kind != "update" or not any(body.standup.model_dump().values())):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       hint="Stand-up fields belong in an update, and at least one is needed.",
                       details={"fields": [{"loc": ["body", "standup"], "type": "not_allowed_for_kind"}]})
    if body.kind == "approval":
        humans = {r[0] for r in c.execute(f"SELECT id FROM principal WHERE kind='human' AND id IN "
                                          f"({','.join('?' * len(to_ids)) or 'NULL'})", to_ids)}
        if not to_ids or set(to_ids) - humans:
            raise ApiError(422, "invalid_request", "The request failed validation.",
                           hint="An approval request names the humans who can grant it (--to).",
                           details={"fields": [{"loc": ["body", "to"], "type": "humans_only"}]})
    if body.drafts and body.kind != "proposal":
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       hint="Task drafts belong in a proposal.",
                       details={"fields": [{"loc": ["body", "drafts"], "type": "not_allowed_for_kind"}]})
    if body.kind == "decision" and (p.kind != "human" or role != "coordinator"):
        raise ApiError(403, "forbidden", "Only a human coordinator may post a decision.")
    return to_ids


@router.post("/sessions/{s}/messages/preflight")
async def preflight_message(s: str, request: Request, key: str = Depends(idempotency_key),
                            recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                            runtime: str | None = Depends(runtime_label),
                            p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Record a draft and its validation outcome without delivering a message."""
    sha = hashlib.sha256()
    captured = bytearray()
    total = 0
    async for chunk in request.stream():
        sha.update(chunk)
        total += len(chunk)
        if len(captured) < 65536:
            captured.extend(chunk[:65536 - len(captured)])
    return record_preflight(database, request.app.state.post_limiter, p, s,
                            bytes(captured), total, sha.hexdigest(), key, recovery, runtime)


def record_preflight(database: db.Database, post_limiter, p: tokens.Principal, s: str, captured: bytes, total: int,
                     draft_hash: str, key: str, recovery: str | None, runtime: str | None):
    """Shared API/browser preflight recorder. `captured` contains at most 64 KiB."""
    with session_tx(database, p, s, runtime) as tx:
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/messages/preflight",
                           {"sha256": draft_hash, "bytes": total})
        if replay is not None:
            return replay
        if tx.me["role"] == "observer":
            raise ApiError(403, "forbidden", "Observers may not preflight messages.")
        require_state(tx.sess, "message")
        if p.kind in ("agent", "client"):
            wait = post_limiter.take(p.id)
            if wait is not None:
                raise ApiError(429, "rate_limited", "Too many preflights from this agent.",
                               hint="Wait for Retry-After seconds.", retryable=True,
                               headers={"Retry-After": str(wait)})
        raw = None
        redacted = False
        issues = []
        parsed = None
        if total > 65536:
            redacted = True
            issues.append({"code": "payload_too_large", "message": "Draft exceeds 64 KiB."})
        else:
            try:
                raw = captured.decode("utf-8", "strict")
            except UnicodeDecodeError:
                redacted = True
                issues.append({"code": "invalid_utf8", "message": "Draft is not UTF-8."})
            if raw is not None:
                try:
                    secret_guard.check(tx.conn, tx.session_id, raw)
                except ApiError:
                    raw = None
                    redacted = True
                    issues.append({"code": "secret_in_content", "message": "Draft contains a board secret."})
            if raw is not None:
                try:
                    document = json.loads(raw)
                except ValueError:
                    issues.append({"code": "invalid_json", "message": "Draft is not valid JSON."})
                else:
                    # Decode JSON escapes before checking: otherwise a secret could
                    # enter the permanent log through an escaped character sequence.
                    try:
                        secret_guard.check(tx.conn, tx.session_id, json.dumps(document, ensure_ascii=False))
                    except ApiError:
                        raw = None
                        redacted = True
                        issues.append({"code": "secret_in_content", "message": "Draft contains a board secret."})
                    if raw is not None:
                        try:
                            parsed = MessageIn.model_validate(document)
                        except ValidationError as exc:
                            issues.append({"code": "invalid_request", "fields": safe_fields(exc.errors())})
                        if parsed is not None:
                            if parsed.preflight_seq is not None:
                                issues.append({"code": "invalid_request", "message": "A draft cannot name a prior preflight."})
                            else:
                                try:
                                    _validate_message(tx.conn, tx.session_id, p, tx.me["role"], parsed)
                                except ApiError as exc:
                                    # Never copy client-supplied details into permanent diagnostics.
                                    issues.append({"code": exc.code, "message": exc.message})
        status = "ready" if not issues else "rejected"
        data = {"status": status, "issues": issues, "redacted": redacted}
        # A digest of withheld text can itself be a credential verifier. Only safe,
        # retained drafts get a public hash; redacted drafts have metadata alone.
        if not redacted:
            data["draft_sha256"] = draft_hash
        if status == "ready":
            data["message_digest"] = _message_digest(parsed)
        seq, text = tx.event("message.preflight", body=raw, data=data,
                             receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "type": "message.preflight",
                                                  "status": status, "issues": issues, "redacted": redacted})
    board_ops.after_write(database, tx.session_id, seq)
    return respond(text, 200)


def send_message(database: db.Database, post_limiter, p: tokens.Principal, s: str, body: MessageIn, key: str,
                 recovery: str | None, runtime: str | None):
    """The one implementation of posting (API and dashboard). Returns the 201 response."""
    replayed = None
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        to_ids = _validate_message(c, sid, p, tx.me["role"], body, semantic=False)
        tx.check_recovery(recovery)
        payload = {"kind": body.kind, "body": body.body, "to": sorted(to_ids), "re": body.re, "task": body.task,
                   "refs": [r.model_dump() for r in body.refs]}           # recipients as sorted IDs
        if body.drafts:                                                   # absent: digests stay as before
            payload["drafts"] = [d.model_dump() for d in body.drafts]
        if body.standup is not None:
            payload["standup"] = body.standup.model_dump()
        if body.preflight_seq is not None:
            payload["preflight_seq"] = body.preflight_seq
        replayed = tx.replay(key, "POST /sessions/{s}/messages", payload, status=201)
        if replayed is not None:
            return replayed
        _validate_message(c, sid, p, tx.me["role"], body)
        require_state(tx.sess, "message")
        if body.preflight_seq is not None:
            pf = c.execute("SELECT actor_id,data FROM event WHERE session_id=? AND seq=? AND type='message.preflight'",
                           (sid, body.preflight_seq)).fetchone()
            if pf is None or pf["actor_id"] != p.id:
                raise ApiError(422, "invalid_preflight", "The preflight must belong to this sender and session.")
            pf_data = json.loads(pf["data"])
            if pf_data.get("status") != "ready" or pf_data.get("message_digest") != _message_digest(body):
                raise ApiError(422, "invalid_preflight", "The message differs from its ready preflight; check it again.")
        if p.kind in ("agent", "client"):
            wait = post_limiter.take(p.id)
            if wait is not None:
                raise ApiError(429, "rate_limited", "Too many posts from this agent.", hint="Wait for Retry-After seconds.",
                               retryable=True, headers={"Retry-After": str(wait)})
        data = {"refs": [r.model_dump(exclude_none=True) for r in body.refs]} if body.refs else {}
        if body.drafts:
            data["drafts"] = [d.model_dump(exclude_none=True) for d in body.drafts]
        if body.standup is not None:
            data["standup"] = body.standup.model_dump(exclude_none=True)
        if body.preflight_seq is not None:
            data["preflight_seq"] = body.preflight_seq
        data = data or None
        seq, text = tx.event(body.kind, body=body.body, data=data, task_num=body.task, reply_to=body.re, to=to_ids,
                             receipt=lambda seq: {"session": tx.sess["handle"], "seq": seq, "type": body.kind})
    board_ops.after_write(database, sid, seq)
    return respond(text, 201)


class ResolveIn(StrictModel):
    resolution: Literal["declined", "obsolete", "answered"]
    note: str | None = Field(default=None, min_length=1, max_length=MAX_BODY_BYTES)


@router.post("/sessions/{s}/questions/{seq}/resolve")
def resolve_question(s: str, seq: int, body: ResolveIn, key: str = Depends(idempotency_key),
                     recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                     runtime: str | None = Depends(runtime_label),
                     p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A recipient resolves its own obligation (scope self); the asker or a coordinator
    resolves every obligation of the question (scope all); if both apply, the asker/coordinator scope wins."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        q = c.execute("SELECT * FROM event WHERE session_id=? AND seq=? AND type='question'", (sid, seq)).fetchone()
        if q is None:
            raise ApiError(404, "not_found", "No such question in this session.")
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/questions/{seq}/resolve", body.model_dump(),
                           target={"type": "event", "id": seq})
        if replay is not None:
            return replay
        holders = inbox.obligation_holders(c, sid, seq)
        is_recipient = c.execute("SELECT 1 FROM event_recipient WHERE session_id=? AND seq=? AND principal_id=?",
                                 (sid, seq, p.id)).fetchone() is not None
        if q["actor_id"] == p.id or tx.me["role"] == "coordinator":
            scope = "all"
            already = not holders and _resolved_all(c, sid, seq)
        elif is_recipient:
            scope = "self"
            already = p.id not in holders
        else:
            raise ApiError(403, "forbidden", "Only a recipient, the asker or a coordinator may resolve this question.")
        require_state(tx.sess, "resolve")
        if already:
            raise ApiError(409, "already_resolved", "This question is already resolved for you.")
        _, text = tx.event("question.resolved", body=body.note, reply_to=seq,
                           data={"question": seq, "resolution": body.resolution, "scope": scope},
                           asker=q["actor_id"], resolved_for=holders if scope == "all" else [],
                           receipt=lambda ev: {"session": tx.sess["handle"], "seq": ev, "question": seq,
                                               "resolution": body.resolution, "scope": scope})
    return respond(text, 200)


class ApprovalIn(StrictModel):
    decision: Literal["grant", "deny"]
    note: str | None = Field(default=None, min_length=1, max_length=MAX_BODY_BYTES)


@router.post("/sessions/{s}/approvals/{seq}")
def decide_approval(s: str, seq: int, body: ApprovalIn, key: str = Depends(idempotency_key),
                    recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                    runtime: str | None = Depends(runtime_label),
                    p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A human the request names, or a human coordinator, grants or denies an approval request once.
    A grant covers exactly what the request said (protocol): it is a recorded human decision, not a role."""
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        q = c.execute("SELECT * FROM event WHERE session_id=? AND seq=? AND type='approval'", (sid, seq)).fetchone()
        if q is None:
            raise ApiError(404, "not_found", "No such approval request in this session.")
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/approvals/{seq}", body.model_dump(), target={"type": "event", "id": seq})
        if replay is not None:
            return replay
        named = c.execute("SELECT 1 FROM event_recipient WHERE session_id=? AND seq=? AND principal_id=?",
                          (sid, seq, p.id)).fetchone() is not None
        if p.kind != "human" or not (named or tx.me["role"] == "coordinator"):
            raise ApiError(403, "forbidden", "Only a human the request names, or a human coordinator, may decide it.")
        require_state(tx.sess, "resolve")
        if c.execute("SELECT 1 FROM event WHERE session_id=? AND reply_to=? AND type IN ('approval.granted','approval.denied')",
                     (sid, seq)).fetchone():
            raise ApiError(409, "already_resolved", "This approval request was already decided.")
        etype = "approval.granted" if body.decision == "grant" else "approval.denied"
        _, text = tx.event(etype, body=body.note, reply_to=seq, task_num=q["task_num"], data={"request": seq},
                           asker=q["actor_id"], resolved_for=[],
                           receipt=lambda ev: {"session": tx.sess["handle"], "seq": ev, "request": seq,
                                               "decision": body.decision})
    return respond(text, 200)


def _resolved_all(c, sid: str, q_seq: int) -> bool:
    """True if nothing is left to resolve: for a broadcast question (no obligations) only an
    earlier scope-all resolution counts; for a directed one, no open obligations."""
    if c.execute("SELECT 1 FROM event_recipient WHERE session_id=? AND seq=?", (sid, q_seq)).fetchone():
        return True
    return c.execute("SELECT 1 FROM event WHERE session_id=? AND reply_to=? AND type='question.resolved' "
                     "AND json_extract(data, '$.scope') = 'all'", (sid, q_seq)).fetchone() is not None


# --- history --------------------------------------------------------------------------------

@router.get("/sessions/{s}/events")
def history(s: str, request: Request, after: int = Query(0, ge=0), before: int | None = Query(None, ge=1),
            task: str | None = Query(None, max_length=12), thread: int | None = Query(None, ge=1),
            actor: str | None = Query(None, max_length=80), type: str | None = Query(None, max_length=40),
            to: Literal["me"] | None = Query(None), limit: int = Query(50, ge=1, le=100),
            format: Literal["json", "text"] = Query("json"),
            p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, me = require_member(c, s, p)
        where, args = ["e.session_id = :s", "e.seq > :after"], {"s": sess["id"], "after": after}
        if before is not None:
            where.append("e.seq < :before"); args["before"] = before
        if task is not None:
            try:
                args["task"] = parse_task(task)
            except ValueError:
                raise ApiError(422, "invalid_request", "The request failed validation.",
                               details={"fields": [{"loc": ["query", "task"], "type": "value_error"}]}) from None
            where.append("e.task_num = :task")
        if thread is not None:
            seqs = inbox.thread_seqs(c, sess["id"], thread)
            where.append(f"e.seq IN ({','.join(str(int(x)) for x in seqs) or 'NULL'})")
        if actor is not None:
            where.append("e.actor_id = (SELECT id FROM principal WHERE handle = :actor)"); args["actor"] = actor
        if type is not None:
            where.append("e.type = :type"); args["type"] = type
        if to == "me":
            where.append("e.actor_id != :p AND (EXISTS (SELECT 1 FROM event_recipient r WHERE r.session_id = e.session_id "
                         "AND r.seq = e.seq AND r.principal_id = :p) OR (e.broadcast = 1 AND e.seq > :joined))")
            args.update(p=p.id, joined=me["joined_seq"])
        rows = c.execute(f"SELECT e.* FROM event e WHERE {' AND '.join(where)} ORDER BY e.seq LIMIT :limit",
                         {**args, "limit": limit + 1}).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        vs = inbox.views(c, rows)
    if format == "text" or _wants_text(request, None):
        lines = [render.section(f"--- history · {sess['handle']} · epoch {sess['epoch']} · head #{sess['head_seq']} ---")]
        for v in vs:
            lines.extend(inbox.view_lines(v))
        if more:
            lines.append(render.section(f"next: more after #{vs[-1]['seq']} (`after={vs[-1]['seq']}`)"))
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"session": sess["handle"], "events": vs, "more": more, "next_after": vs[-1]["seq"] if (more and vs) else None}


# --- export ---------------------------------------------------------------------------------

@router.get("/sessions/{s}/export")
def export(s: str, format: Literal["md", "jsonl"] = Query("md"),
           p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    """The whole session: canonical text (md) or one JSON event per line (jsonl). Never receipts,
    digests, op IDs, fence or consumer secrets."""
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        vs = inbox.views(c, c.execute("SELECT * FROM event WHERE session_id=? ORDER BY seq", (sess["id"],)))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    name = f"glassboard-{sess['handle']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}"
    if format == "jsonl":
        head = {"session": sess["handle"], "title": sess["title"], "state": sess["state"], "epoch": sess["epoch"],
                "head": sess["head_seq"], "exported_at": stamp}
        text = "\n".join(json.dumps(x, ensure_ascii=True, separators=(",", ":")) for x in [head, *vs]) + "\n"
        return Response(text, media_type="application/x-ndjson",
                        headers={"Content-Disposition": f'attachment; filename="{name}.jsonl"'})
    lines = [render.section(f"--- glassboard export · {sess['handle']} · {sess['state']} · epoch {sess['epoch']} "
                            f"· head #{sess['head_seq']} · exported {stamp} ---"),
             render.inline_line("title", sess["title"])]
    for v in vs:
        lines.extend(inbox.view_lines(v))
    return Response("\n".join(lines) + "\n", media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
