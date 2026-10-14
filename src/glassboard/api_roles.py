"""Versioned job-role prompts. Job roles are descriptive; member.role controls permissions."""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends
from pydantic import Field

from . import db, ids, render, secret_guard, sessions, tokens
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import check_replay, digest, idempotency_key, receipt_body, respond
from .models import StrictModel
from .mutation import runtime_label, session_tx
from fastapi import Header

router = APIRouter(prefix="/api/v1/roles")
session_router = APIRouter(prefix="/api/v1/sessions")
ROLE_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"


def _admin(p: tokens.Principal) -> None:
    if p.kind != "human" or not p.is_admin or p.scope != "full":
        raise ApiError(403, "forbidden", "Only a full-scope human administrator can edit role prompts.")


def list_roles_data(c) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT r.name, r.version, r.prompt, r.updated_at, p.handle updated_by "
        "FROM role_template r JOIN principal p ON p.id=r.updated_by ORDER BY r.name")]


@router.get("")
def list_roles(p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        return {"roles": list_roles_data(c)}


@router.get("/{name}/revisions")
def role_revisions(name: str, p: tokens.Principal = Depends(authenticate),
                   database: db.Database = Depends(get_db)):
    with database.read() as c:
        rows = c.execute(
            "SELECT r.name,r.version,r.prompt,r.updated_at,p.handle updated_by "
            "FROM role_template_revision r JOIN principal p ON p.id=r.updated_by "
            "WHERE r.name=? ORDER BY r.version DESC", (name,)).fetchall()
        if not rows:
            raise ApiError(404, "not_found", "No such job role.")
        return {"name": name, "revisions": [dict(r) for r in rows]}


class RoleUpdate(StrictModel):
    prompt: str = Field(min_length=1, max_length=4000)
    if_version: int = Field(ge=0)


def update_role(database: db.Database, p: tokens.Principal, name: str, body: RoleUpdate, key: str):
    _admin(p)
    import re
    if not re.fullmatch(ROLE_PATTERN, name):
        raise ApiError(422, "invalid_request", "The role name is invalid.")
    if render.r2(body.prompt, allow_newline=True) != body.prompt:
        raise ApiError(422, "invalid_request", "The role prompt contains hidden or control characters.")
    with database.write() as c:
        secret_guard.check(c, None, body.prompt)
        d = digest("PUT /roles/{name}", p.id, {**body.model_dump(), "name": name},
                   target={"type": "role", "id": name})
        prior = c.execute("SELECT req_digest, receipt FROM audit WHERE actor_id=? AND op_id=?",
                          (p.id, key)).fetchone()
        replay = check_replay(prior, d, 200)
        if replay is not None:
            return replay
        current = c.execute("SELECT version FROM role_template WHERE name=?", (name,)).fetchone()
        version = current["version"] if current else 0
        if version != body.if_version:
            raise ApiError(409, "version_conflict", "This role prompt changed; reload it before editing.")
        next_version, now = version + 1, db.utcnow()
        if current:
            c.execute("UPDATE role_template SET version=?,prompt=?,updated_at=?,updated_by=? WHERE name=?",
                      (next_version, body.prompt, now, p.id, name))
        else:
            c.execute("INSERT INTO role_template(name,version,prompt,updated_at,updated_by) VALUES (?,?,?,?,?)",
                      (name, next_version, body.prompt, now, p.id))
        c.execute("INSERT INTO role_template_revision(name,version,prompt,updated_at,updated_by) VALUES (?,?,?,?,?)",
                  (name, next_version, body.prompt, now, p.id))
        text = receipt_body({"name": name, "version": next_version, "updated_at": now})
        c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id,receipt,req_digest) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (ids.new_ulid(), now, p.id, "role_template.update", name,
                   json.dumps({"version": next_version}), key, text, d))
    return respond(text, 200)


@router.put("/{name}")
def put_role(name: str, body: RoleUpdate, key: str = Depends(idempotency_key),
             p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    return update_role(database, p, name, body, key)


@session_router.get("/{s}/job-roles")
def session_job_roles(s: str, p: tokens.Principal = Depends(authenticate),
                      database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = sessions.require_member(c, s, p)
        rows = c.execute(
            "SELECT pr.handle, m.role permission, m.job_role, m.job_role_version, "
            "r.prompt effective_prompt "
            "FROM member m JOIN principal pr ON pr.id=m.principal_id "
            "LEFT JOIN role_template_revision r ON r.name=m.job_role AND r.version=m.job_role_version "
            "WHERE m.session_id=? AND m.left_at IS NULL ORDER BY pr.handle", (sess["id"],)).fetchall()
    return {"session": sess["handle"], "assignments": [dict(r) for r in rows]}


class JobRoleSet(StrictModel):
    handle: str = Field(pattern=sessions.PRINCIPAL_HANDLE_RE.pattern)
    role: str | None = Field(default=None, pattern=ROLE_PATTERN)


@session_router.post("/{s}/job-roles")
def set_job_role(s: str, body: JobRoleSet, key: str = Depends(idempotency_key),
                 recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                 runtime: str | None = Depends(runtime_label),
                 p: tokens.Principal = Depends(require_full_scope), database: db.Database = Depends(get_db)):
    with session_tx(database, p, s, runtime) as tx:
        c = tx.conn
        target = c.execute("SELECT m.*, pr.handle, pr.kind FROM member m JOIN principal pr ON pr.id=m.principal_id "
                           "WHERE m.session_id=? AND pr.handle=? AND m.left_at IS NULL",
                           (tx.session_id, body.handle)).fetchone()
        if target is None:
            raise ApiError(404, "not_found", "No such member.")
        tx.check_recovery(recovery)
        template = c.execute("SELECT version FROM role_template WHERE name=?", (body.role,)).fetchone() if body.role else None
        if body.role and template is None:
            raise ApiError(404, "not_found", "No such job role.")
        role_version = template["version"] if template else None
        replay = tx.replay(key, "POST /sessions/{s}/job-roles",
                           {"principal": target["principal_id"], "role": body.role})
        if replay is not None:
            return replay
        if tx.me["role"] == "observer" or (target["principal_id"] != p.id and tx.me["role"] != "coordinator"):
            raise ApiError(403, "forbidden", "Only yourself or a coordinator can set this job role.")
        if target["kind"] == "human" and target["principal_id"] != p.id and p.kind != "human":
            raise ApiError(403, "forbidden", "An agent cannot assign a human job role.")
        sessions.require_state(tx.sess, "member.add")
        if target["job_role"] == body.role and target["job_role_version"] == role_version:
            return respond(receipt_body({"handle": target["handle"], "role": body.role,
                                         "version": role_version, "change": "none", "seq": None}), 200)
        c.execute("UPDATE member SET job_role=?,job_role_version=? WHERE session_id=? AND principal_id=?",
                  (body.role, role_version, tx.session_id, target["principal_id"]))
        _, text = tx.event("member.jobrole", member=target["principal_id"],
                           data={"handle": target["handle"], "job_role": body.role or "none",
                                 "role_version": role_version},
                           receipt=lambda seq: {"handle": target["handle"], "role": body.role,
                                                "version": role_version, "change": "set", "seq": seq})
        return respond(text, 200)
