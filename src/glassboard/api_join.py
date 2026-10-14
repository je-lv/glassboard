"""Joining agents with one-time invite codes.

A human coordinator adds one of their OWN agents to a session (creating the agent if needed) and gets
an invite code: 12 base32 characters shown as `ABCD-EFGH-JKLM` (60 bits), single use, valid for 15
minutes. Only its SHA-256 is stored. The agent redeems it with `POST /api/v1/join` (no token needed):
the board mints a fresh token for that agent and returns it once, with the session and its recovery
ID. The code never reaches a log, an event or an error message. Redeeming is unauthenticated, so every
failure counts against the same per-address limiter as a bad token.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field

from . import db, ids, inbox, tokens
from .deps import client_addr, get_db, require_full_scope
from .errors import ApiError
from .idempotency import check_replay, idempotency_key, receipt_body, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from .sessions import PRINCIPAL_HANDLE_RE, require_state

router = APIRouter(prefix="/api/v1")
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"      # base32: 5 bits per character
INVITE_TTL = timedelta(minutes=15)
SEG = r"^[a-z0-9-]{1,32}$"


def _norm(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash(code: str) -> bytes:
    return hashlib.sha256(_norm(code).encode()).digest()


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


class InviteIn(StrictModel):
    agent: str = Field(pattern=SEG)                       # the agent's name under the caller: jose/<agent>
    role: str = Field(default="member", pattern=r"^(member|coordinator|observer)$")   # agent coordinator = Task Manager


@router.post("/sessions/{s}/invites", status_code=201)
def create_invite(s: str, body: InviteIn, request: Request, key: str = Depends(idempotency_key),
                  recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                  runtime: str | None = Depends(runtime_label),
                  p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """A human coordinator invites one of their own agents (created on the fly if it does not exist).
    Adding the membership is the session event (`member.added`); the invite itself is an audit row.
    The code appears only in this response (an idempotent replay shows `code: null`)."""
    code = new_code()
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        handle = f"{p.handle}/{body.agent}"
        tx.check_recovery(recovery)
        replay = tx.replay(key, "POST /sessions/{s}/invites", {"agent": handle, "role": body.role}, status=201)
        if replay is not None:
            return replay
        # The same request again (e.g. a resubmitted form) never makes a second code, even when nothing in
        # the session changed (no event, so no event receipt): the invite's audit row keeps the receipt.
        prior = c.execute("SELECT req_digest, receipt FROM audit WHERE actor_id=? AND op_id=? AND action='invite.create'",
                          (p.id, key)).fetchone()
        again = check_replay(prior, tx.digest, 201)
        if again is not None:
            return again
        if p.kind != "human" or tx.me["role"] != "coordinator":     # after membership: non-members get 404
            raise ApiError(403, "forbidden", "Only a coordinator of this session invites agents.")
        require_state(tx.sess, "member.add")
        now = datetime.now(timezone.utc)
        agent = c.execute("SELECT * FROM principal WHERE handle=?", (handle,)).fetchone()
        created = False
        if agent is None:
            aid = ids.new_ulid()
            c.execute("INSERT INTO principal(id,handle,kind,owner_id,display_name,created_at) VALUES (?,?,?,?,?,?)",
                      (aid, handle, "agent", p.id, handle, db.utcnow()))
            agent = c.execute("SELECT * FROM principal WHERE id=?", (aid,)).fetchone()
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), p.id, "principal.create", aid,
                       json.dumps({"handle": handle, "kind": "agent", "via": "invite"})))
            created = True
        elif agent["owner_id"] != p.id or not agent["active"]:
            raise ApiError(404, "not_found", "No such agent of yours.")
        member = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                           (sid, agent["id"])).fetchone()
        receipt = {"agent": handle, "session": tx.sess["handle"], "created_agent": created, "code": None,
                   "expires_at": (now + INVITE_TTL).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        if member is None:
            seq, _ = tx.event("member.added", member=agent["id"], data={"handle": handle, "role": body.role},
                              receipt=lambda seq: {**receipt, "seq": seq})
            prev = c.execute("SELECT 1 FROM member WHERE session_id=? AND principal_id=?", (sid, agent["id"])).fetchone()
            if prev is None:
                c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                          " VALUES (?,?,?,?,?,?,?)", (sid, agent["id"], body.role, seq, seq - 1, seq - 1, db.utcnow()))
            else:
                c.execute("UPDATE member SET role=?, job_role=NULL, job_role_version=NULL, joined_seq=?, cursor_seq=?, delivered_seq=?, joined_at=?, left_at=NULL,"
                          " consumer_secret=NULL, consumer_runtime=NULL, consumer_since=NULL, consumer_gen=consumer_gen+1"
                          " WHERE session_id=? AND principal_id=?", (body.role, seq, seq - 1, seq - 1, db.utcnow(), sid, agent["id"]))
        else:
            # Already a member: nothing changes in the session, so no event (and no stored receipt: a retried
            # request simply makes another single-use invite, which is harmless).
            seq = None
        c.execute("INSERT INTO invite(id,code_sha256,agent_id,session_id,created_by,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                  (ids.new_ulid(), _hash(code), agent["id"], sid, p.id, db.utcnow(), receipt["expires_at"]))
        c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id,receipt,req_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ids.new_ulid(), db.utcnow(), p.id, "invite.create", agent["id"],
                   json.dumps({"session": tx.sess["handle"], "expires_at": receipt["expires_at"]}), key,
                   receipt_body({**receipt, "seq": seq}), tx.digest))
    # The only response that carries the code.
    return respond(receipt_body({**receipt, "seq": seq, "code": code}), 201)


class JoinIn(StrictModel):
    code: str = Field(min_length=8, max_length=32)


@router.post("/join")
def join(body: JoinIn, request: Request, database: db.Database = Depends(get_db)):
    """Redeem an invite: a fresh token for the invited agent, returned once."""
    limiter, addr = request.app.state.auth_limiter, client_addr(request)
    wait = limiter.retry_after(addr)
    if wait is not None:
        raise ApiError(429, "rate_limited", "Too many failed attempts from this address.", retryable=True,
                       headers={"Retry-After": str(wait)})
    bad = ApiError(404, "invite_invalid", "This invite code is unknown, already used or expired.",
                   hint="Ask your human for a new one.")
    with database.write() as c:
        inv = c.execute("SELECT * FROM invite WHERE code_sha256=?", (_hash(body.code),)).fetchone()
        now = db.utcnow()
        if inv is None or inv["used_at"] is not None or inv["expires_at"] < now:
            limiter.record_failure(addr)
            raise bad
        agent = c.execute("SELECT * FROM principal WHERE id=?", (inv["agent_id"],)).fetchone()
        sess = c.execute("SELECT * FROM session WHERE id=?", (inv["session_id"],)).fetchone()
        owner = c.execute("SELECT active FROM principal WHERE id=?", (agent["owner_id"],)).fetchone()
        if not agent["active"] or (owner is not None and not owner["active"]) or sess["state"] == "archived":
            limiter.record_failure(addr)
            raise bad
        c.execute("UPDATE invite SET used_at=? WHERE id=?", (now, inv["id"]))
        tok, cid, secret_hash = tokens.new_token()
        c.execute("INSERT INTO credential(id,principal_id,secret_sha256,scope,label,created_at) VALUES (?,?,?,?,?,?)",
                  (cid, agent["id"], secret_hash, "full", f"invite {sess['handle']}", now))
        c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                  (ids.new_ulid(), now, agent["id"], "invite.redeem", cid, json.dumps({"session": sess["handle"]})))
    return respond(receipt_body({"token": tok, "handle": agent["handle"], "credential_id": cid,
                                 "session": sess["handle"], "recovery_id": sess["recovery_id"]}), 200)
