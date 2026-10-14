"""Sessions and membership.

Check order on every session route: authenticate → membership (404, also for unknown
sessions, so non-members learn nothing) → referenced objects (404) → recovery (428/409) →
role/kind (403) → state (409). Mutations run every check inside the write transaction.
Membership and session creation are human-only operations; agents get 403.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from pydantic import Field, StrictBool

from . import db, events, ids, journal, tokens
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import check_replay, digest, idempotency_key, receipt_body, respond
from .mutation import runtime_label, session_tx
from .models import StrictModel
from .protocol import PROTOCOL_VERSION

router = APIRouter(prefix="/api/v1")

from .sessions import (PRINCIPAL_HANDLE_RE, RECOVERY_ID_CHARS, ROLE_RANK, SESSION_HANDLE_RE,  # noqa: E402
                       _human_coordinators, active_member, check_recovery, find_session, journal_error,
                       my_sessions, require_human_coordinator, require_member, require_state, session_view)
from .sessions import TRANSITIONS  # noqa: E402


# --- sessions --------------------------------------------------------------------------------------

class SessionCreate(StrictModel):
    handle: str = Field(pattern=SESSION_HANDLE_RE.pattern)
    title: str = Field(min_length=1, max_length=200)
    stale_after_s: int = Field(default=1800, ge=300, le=14400)
    autonomous: StrictBool = False


@router.post("/sessions", status_code=201)
def create_session(body: SessionCreate, request: Request, key: str = Depends(idempotency_key),
                   p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Any active human may create a session and becomes its first coordinator. One
    `session.created` event (seq 1) records it; the audit row carries the instance-level receipt
    and Idempotency-Key, since no session exists yet to scope the key."""
    if p.kind != "human":
        raise ApiError(403, "forbidden", "Only humans may create sessions.")
    payload = body.model_dump()
    d = digest("POST /sessions", p.id, payload)
    with database.write() as c:
        prior = c.execute("SELECT req_digest, receipt FROM audit WHERE actor_id=? AND op_id=?", (p.id, key)).fetchone()
        replay = check_replay(prior, d, 201)
        if replay is not None:
            return replay
        if c.execute("SELECT 1 FROM session WHERE handle=?", (body.handle,)).fetchone():
            raise ApiError(409, "handle_taken", "A session with this handle already exists.")
        if c.execute("SELECT 1 FROM workspace_purge WHERE session_handle=?", (body.handle,)).fetchone():
            raise ApiError(409, "handle_taken", "That session handle belongs to a purged workspace and remains reserved.")
        sid, rid, now = ids.new_ulid(), ids.new_random_b32(RECOVERY_ID_CHARS), db.utcnow()
        c.execute("INSERT INTO session(id,handle,title,state,recovery_id,epoch,head_seq,next_task_num,stale_after_s,"
                  "autonomous,protocol_version,created_at,updated_at) VALUES (?,?,?,?,?,1,0,1,?,?,?,?,?)",
                  (sid, body.handle, body.title, "active", rid, body.stale_after_s,
                   int(body.autonomous), PROTOCOL_VERSION, now, now))
        c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                  " VALUES (?,?,'coordinator',1,0,0,?)", (sid, p.id, now))
        sess = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()

        def build(seq):
            return receipt_body({**session_view(sess, "coordinator"), "head": seq, "seq": seq})
        seq, text = events.append(c, sid, p.id, "session.created", broadcast=True, op_id=key, req_digest=d,
                                  data={"title": body.title, "coordinator": p.handle,
                                        "autonomous": bool(body.autonomous)}, receipt=build)
        c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id,receipt,req_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ids.new_ulid(), now, p.id, "session.create", sid, json.dumps({"handle": body.handle}), key, text, d))
    return respond(text, 201)


@router.get("/sessions")
def list_sessions(p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        return {"sessions": my_sessions(c, p.id)}


# --- membership --------------------------------------------------------------------------------

def member_view(row) -> dict:
    # never the consumer secret; only the non-secret prefix C<gen>.
    return {"handle": row["handle"], "kind": row["kind"], "role": row["role"], "joined_seq": row["joined_seq"],
            "last_seen_at": row["last_seen_at"],
            "consumer": f"C{row['consumer_gen']}" if row["consumer_secret"] else None,
            "consumer_runtime": row["consumer_runtime"] if row["consumer_secret"] else None}


@router.get("/sessions/{s}/members")
def list_members(s: str, p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        rows = c.execute("SELECT m.*, pr.handle, pr.kind FROM member m JOIN principal pr ON pr.id = m.principal_id "
                         "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY pr.handle", (sess["id"],))
        return {"session": sess["handle"], "members": [member_view(r) for r in rows]}


class MemberSet(StrictModel):
    handle: str = Field(pattern=PRINCIPAL_HANDLE_RE.pattern)
    role: Literal["coordinator", "member", "observer"] = "member"


def _target_principal(c, handle: str):
    t = c.execute("SELECT * FROM principal WHERE handle=? AND kind!='system'", (handle,)).fetchone()
    if t is None or not t["active"]:
        raise ApiError(404, "not_found", "No such active principal.")
    return t


def _guard_last_coordinator(c, session_id: str, target_id: str) -> None:
    if _human_coordinators(c, session_id) == [target_id]:
        raise ApiError(409, "invalid_transition", "This would leave the session without a human coordinator.",
                       hint="Make another human a coordinator first.")


@router.post("/sessions/{s}/members")
def set_member(s: str, body: MemberSet, request: Request, key: str = Depends(idempotency_key),
               recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
               runtime: str | None = Depends(runtime_label),
               p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    """Add a principal, re-add one who left, or change a member's role. Role
    reductions are journaled first; grants are not."""
    jpath = request.app.state.journal_path
    try:
        with session_tx(database, p, s, runtime) as tx:
            c = tx.conn
            target = _target_principal(c, body.handle)
            tx.check_recovery(recovery)
            replay = tx.replay(key, "POST /sessions/{s}/members", {"principal": target["id"], "role": body.role})
            if replay is not None:
                return replay
            require_human_coordinator(p, tx.me)
            sid, now = tx.session_id, db.utcnow()
            row = c.execute("SELECT * FROM member WHERE session_id=? AND principal_id=?", (sid, target["id"])).fetchone()
            reducing = row is not None and row["left_at"] is None and ROLE_RANK[body.role] < ROLE_RANK[row["role"]]
            require_state(tx.sess, "member.reduce" if reducing else "member.add")
            if row is not None and row["left_at"] is None:
                old = row["role"]
                if old == body.role:          # no change → no event, nothing to replay
                    return respond(receipt_body({"member": target["handle"], "role": old, "change": "none", "seq": None}), 200)
                if ROLE_RANK[body.role] < ROLE_RANK[old]:
                    if target["kind"] == "human" and old == "coordinator":
                        _guard_last_coordinator(c, sid, target["id"])
                    e = journal.append(jpath, "member.role_reduce", session_id=sid, principal_id=target["id"], role=body.role)
                    journal.apply_entry(c, e)
                else:
                    c.execute("UPDATE member SET role=? WHERE session_id=? AND principal_id=?", (body.role, sid, target["id"]))
                _, text = tx.event("member.changed", member=target["id"],
                                   data={"handle": target["handle"], "from": old, "to": body.role},
                                   receipt=lambda seq: {"member": target["handle"], "role": body.role,
                                                        "change": "role_changed", "seq": seq})
                return respond(text, 200)
            # New member, or re-added after leaving: joined_seq is the member.added event itself, the
            # cursor starts just before it, so the member's inbox begins with its own member.added.
            seq, text = tx.event("member.added", member=target["id"], data={"handle": target["handle"], "role": body.role},
                                 receipt=lambda seq: {"member": target["handle"], "role": body.role,
                                                      "change": "added", "seq": seq})
            if row is None:
                c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                          " VALUES (?,?,?,?,?,?,?)", (sid, target["id"], body.role, seq, seq - 1, seq - 1, now))
            else:
                c.execute("UPDATE member SET role=?, job_role=NULL, job_role_version=NULL, joined_seq=?, cursor_seq=?, delivered_seq=?, joined_at=?, left_at=NULL,"
                          " consumer_secret=NULL, consumer_runtime=NULL, consumer_since=NULL, consumer_gen=consumer_gen+1"
                          " WHERE session_id=? AND principal_id=?",
                          (body.role, seq, seq - 1, seq - 1, now, sid, target["id"]))
            return respond(text, 200)
    except journal.JournalWriteError as e:
        raise journal_error(e) from None


@router.delete("/sessions/{s}/members/{handle:path}")
def remove_member(s: str, handle: str, request: Request, key: str = Depends(idempotency_key),
                  recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                  runtime: str | None = Depends(runtime_label),
                  p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    if not PRINCIPAL_HANDLE_RE.match(handle):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       details={"fields": [{"loc": ["path", "handle"], "type": "string_pattern_mismatch"}]})
    jpath = request.app.state.journal_path
    try:
        with session_tx(database, p, s, runtime) as tx:
            c = tx.conn
            # Referenced object: the principal (404 if unknown). Whether it is still an active
            # member is a state check after the replay lookup, so a retried removal that already
            # committed gets its receipt rather than "no such member".
            target = c.execute("SELECT * FROM principal WHERE handle=? AND kind!='system'", (handle,)).fetchone()
            if target is None:
                raise ApiError(404, "not_found", "No such member.")
            tx.check_recovery(recovery)
            replay = tx.replay(key, "DELETE /sessions/{s}/members/{handle}", {},
                               target={"type": "principal", "id": target["id"]})
            if replay is not None:
                return replay
            row = active_member(c, tx.session_id, target["id"])
            require_human_coordinator(p, tx.me)
            require_state(tx.sess, "member.reduce")
            if row is None:
                raise ApiError(404, "not_found", "No such member.")
            sid = tx.session_id
            if target["kind"] == "human" and row["role"] == "coordinator":
                _guard_last_coordinator(c, sid, target["id"])
            e = journal.append(jpath, "member.remove", session_id=sid, principal_id=target["id"])
            journal.apply_entry(c, e)
            c.execute("UPDATE member SET consumer_secret=NULL, consumer_runtime=NULL, consumer_since=NULL "
                      "WHERE session_id=? AND principal_id=?", (sid, target["id"]))
            _, text = tx.event("member.removed", member=target["id"], data={"handle": target["handle"]},
                               receipt=lambda seq: {"member": target["handle"], "removed": True, "seq": seq})
            return respond(text, 200)
    except journal.JournalWriteError as e:
        raise journal_error(e) from None


# --- session state ----------------------------------------------------------------------------

class StateIn(StrictModel):
    state: Literal["active", "paused", "archived"]
    reason: str = Field(min_length=1, max_length=2000)


def set_state(database: db.Database, jpath: str, p: tokens.Principal, s: str, body: StateIn, key: str,
              recovery: str | None, runtime: str | None):
    """Human coordinators only. active ↔ paused, active|paused → archived, archived → active
    (unarchive, explicit). Archiving is journaled (session.archive); the rest are not."""
    try:
        with session_tx(database, p, s, runtime) as tx:
            c, sid = tx.conn, tx.session_id
            tx.check_recovery(recovery)
            replay = tx.replay(key, "POST /sessions/{s}/state", body.model_dump())
            if replay is not None:
                return replay
            require_human_coordinator(p, tx.me)
            old = tx.sess["state"]
            etype = TRANSITIONS.get((old, body.state))
            if etype is None:
                raise ApiError(409, "invalid_transition", f"A session cannot go from {old} to {body.state}.")
            if body.state == "archived":
                e = journal.append(jpath, "session.archive", session_id=sid)
                journal.apply_entry(c, e)
            else:
                c.execute("UPDATE session SET state=?, updated_at=? WHERE id=?", (body.state, db.utcnow(), sid))
            _, text = tx.event(etype, body=body.reason, data={"from": old, "to": body.state}, capped=False,
                               receipt=lambda seq: {"session": tx.sess["handle"], "state": body.state, "from": old,
                                                    "seq": seq})
        return respond(text, 200)
    except journal.JournalWriteError as e:
        raise journal_error(e) from None


@router.post("/sessions/{s}/state")
def change_state(s: str, body: StateIn, request: Request, key: str = Depends(idempotency_key),
                 recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                 runtime: str | None = Depends(runtime_label),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return set_state(database, request.app.state.journal_path, p, s, body, key, recovery, runtime)
