"""Self-service for humans: register owned agents, mint and revoke tokens.

Instance-level operations: one `audit` row each, no session event, no recovery
header. The audit row stores the receipt and the request digest under UNIQUE(actor_id, op_id)
(the idempotency key scope for audit rows). Token creation is the one receipt without its secret: the
first response shows the token once; a replay returns `secret: null, secret_already_shown: true`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from . import db, ids, journal, tokens
from .sessions import journal_error
from .deps import get_db, require_full_scope
from .errors import ApiError
from .idempotency import check_replay, digest, idempotency_key, receipt_body, respond
from .models import StrictModel

router = APIRouter(prefix="/api/v1/me")
SEG = r"^[a-z0-9-]{1,32}$"
HANDLE = r"^[a-z0-9-]{1,32}(/[a-z0-9-]{1,32})?$"


def _human(p: tokens.Principal) -> None:
    if p.kind != "human":
        raise ApiError(403, "forbidden", "Only humans may manage agents and tokens.")


def _audit(c, actor_id, action, target, data, key, receipt, d):
    c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id,receipt,req_digest) VALUES (?,?,?,?,?,?,?,?,?)",
              (ids.new_ulid(), db.utcnow(), actor_id, action, target, json.dumps(data), key, receipt, d))


def _prior(c, actor_id, key):
    return c.execute("SELECT req_digest, receipt FROM audit WHERE actor_id=? AND op_id=?", (actor_id, key)).fetchone()


class AgentCreate(StrictModel):
    name: str = Field(pattern=SEG)
    kind: Literal["agent", "client"] = "agent"
    display_name: str | None = Field(default=None, min_length=1, max_length=64)


@router.post("/agents", status_code=201)
def create_agent(body: AgentCreate, key: str = Depends(idempotency_key),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    _human(p)
    d = digest("POST /me/agents", p.id, body.model_dump())
    with database.write() as c:
        replay = check_replay(_prior(c, p.id, key), d, 201)
        if replay is not None:
            return replay
        handle = f"{p.handle}/{body.name}"
        if c.execute("SELECT 1 FROM principal WHERE handle=?", (handle,)).fetchone():
            raise ApiError(409, "handle_taken", "You already have an agent with this name.")
        pid = ids.new_ulid()
        c.execute("INSERT INTO principal(id,handle,kind,owner_id,display_name,created_at) VALUES (?,?,?,?,?,?)",
                  (pid, handle, body.kind, p.id, body.display_name or handle, db.utcnow()))
        text = receipt_body({"handle": handle, "id": pid, "kind": body.kind, "owner": p.handle})
        _audit(c, p.id, "principal.create", pid, {"handle": handle, "kind": body.kind}, key, text, d)
    return respond(text, 201)


class TokenCreate(StrictModel):
    principal: str | None = Field(default=None, pattern=HANDLE)   # default: myself
    scope: Literal["full", "read"] = "full"
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    label: str | None = Field(default=None, min_length=1, max_length=64)


def _owned(c, p: tokens.Principal, handle: str | None):
    """The caller itself, or an active agent/client the caller owns. Anything else → 404."""
    if handle is None or handle == p.handle:
        return c.execute("SELECT * FROM principal WHERE id=?", (p.id,)).fetchone()
    row = c.execute("SELECT * FROM principal WHERE handle=? AND owner_id=? AND active=1", (handle, p.id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such principal of yours.")
    return row


@router.post("/tokens", status_code=201)
def create_token(body: TokenCreate, key: str = Depends(idempotency_key),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    _human(p)
    with database.write() as c:
        target = _owned(c, p, body.principal)
        # handles are hashed as immutable IDs.
        payload = {**body.model_dump(), "principal": target["id"]}
        d = digest("POST /me/tokens", p.id, payload)
        replay = check_replay(_prior(c, p.id, key), d, 201)
        if replay is not None:
            return replay
        tok, cid, secret_hash = tokens.new_token()
        exp = None
        if body.expires_days is not None:
            exp = (datetime.now(timezone.utc) + timedelta(days=body.expires_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        c.execute("INSERT INTO credential(id,principal_id,secret_sha256,scope,label,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                  (cid, target["id"], secret_hash, body.scope, body.label, db.utcnow(), exp))
        view = {"id": cid, "principal": target["handle"], "scope": body.scope, "expires_at": exp}
        stored = receipt_body({**view, "secret": None, "secret_already_shown": True})
        _audit(c, p.id, "credential.create", cid, {"principal_id": target["id"], "scope": body.scope}, key, stored, d)
    # The only response that ever contains the secret; it is not stored anywhere.
    return respond(receipt_body({**view, "secret": tok, "secret_already_shown": False}), 201)


@router.delete("/tokens/{credential_id}")
def revoke_token(credential_id: str, request: Request, key: str = Depends(idempotency_key),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    _human(p)
    jpath = request.app.state.journal_path
    try:
        with database.write() as c:
            row = c.execute("SELECT c.id, c.revoked_at, pr.id pid, pr.owner_id FROM credential c "
                            "JOIN principal pr ON pr.id = c.principal_id WHERE c.id=?", (credential_id,)).fetchone()
            if row is None or (row["pid"] != p.id and row["owner_id"] != p.id):
                raise ApiError(404, "not_found", "No such credential of yours.")
            d = digest("DELETE /me/tokens/{id}", p.id, {}, target={"type": "credential", "id": row["id"]})
            replay = check_replay(_prior(c, p.id, key), d, 200)
            if replay is not None:
                return replay
            already = row["revoked_at"] is not None
            if not already:
                e = journal.append(jpath, "credential.revoke", credential_id=row["id"])
                journal.apply_entry(c, e)
            text = receipt_body({"id": row["id"], "revoked": True})
            _audit(c, p.id, "credential.revoke", row["id"], {"already_revoked": already}, key, text, d)
        return respond(text, 200)
    except journal.JournalWriteError as e:
        raise journal_error(e) from None
