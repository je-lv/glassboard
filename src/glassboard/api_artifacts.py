"""Immutable, session-scoped evidence files.

Artifacts are deliberately small and inert. Glassboard stores their bytes in SQLite, serves them
only as downloads, and records an `artifact.added` event. It never extracts or renders them.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import urllib.parse

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response

from . import db, ids, secret_guard, tokens
from .api_messages import parse_task
from .deps import authenticate, get_db, require_full_scope
from .errors import ApiError
from .idempotency import idempotency_key, respond
from .mutation import runtime_label, session_tx
from .sessions import require_member

router = APIRouter(prefix="/api/v1")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SESSION_ARTIFACT_BYTES = 512 * 1024 * 1024
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
MEDIA_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
PURPOSES = {"evidence", "deliverable"}


def _metadata(name: str, media_type: str, description: str | None, purpose: str) -> None:
    if not NAME_RE.fullmatch(name) or "/" in name or "\\" in name or name in (".", ".."):
        raise ApiError(422, "invalid_request", "Artifact name must be a plain file name without control characters.")
    if len(media_type) > 127 or not MEDIA_RE.fullmatch(media_type):
        raise ApiError(422, "invalid_request", "Artifact media type is invalid.")
    if description is not None and (not description.strip() or len(description) > 2000):
        raise ApiError(422, "invalid_request", "Artifact description must contain 1 to 2000 characters.")
    if purpose not in PURPOSES:
        raise ApiError(422, "invalid_request", "Artifact purpose must be evidence or deliverable.")


def _task_num(conn, sid: str, task: str | None) -> int | None:
    if task is None:
        return None
    try:
        num = parse_task(task)
    except ValueError:
        raise ApiError(404, "not_found", "No such task in this session.") from None
    if conn.execute("SELECT 1 FROM task WHERE session_id=? AND num=?", (sid, num)).fetchone() is None:
        raise ApiError(404, "not_found", "No such task in this session.")
    return num


def artifact_view(conn, row) -> dict:
    creator = conn.execute("SELECT handle FROM principal WHERE id=?", (row["created_by"],)).fetchone()
    return {"id": row["id"], "name": row["name"], "media_type": row["media_type"],
            "description": row["description"], "size": row["size"], "sha256": row["sha256"],
            "purpose": row["purpose"],
            "task": row["task_num"], "created_by": creator["handle"] if creator else None,
            "created_at": row["created_at"], "event_seq": row["event_seq"]}


def create_artifact(database: db.Database, p: tokens.Principal, s: str, content: bytes, *, name: str,
                    media_type: str, description: str | None, task: str | None, purpose: str, key: str,
                    recovery: str | None, runtime: str | None):
    _metadata(name, media_type, description, purpose)
    if not content:
        raise ApiError(422, "invalid_request", "Artifact content cannot be empty.")
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ApiError(413, "payload_too_large", "Artifact exceeds the 16 MiB limit.")
    digest = hashlib.sha256(content).hexdigest()
    with session_tx(database, p, s, runtime) as tx:
        c, sid = tx.conn, tx.session_id
        num = _task_num(c, sid, task)
        tx.check_recovery(recovery)
        payload = {"name": name, "media_type": media_type, "description": description, "task": num,
                   "purpose": purpose,
                   "size": len(content), "sha256": digest}
        replay = tx.replay(key, "POST /sessions/{s}/artifacts", payload, status=201)
        if replay is not None:
            return replay
        if tx.me["role"] == "observer":
            raise ApiError(403, "forbidden", "Observers cannot upload artifacts.")
        used = c.execute("SELECT COALESCE(SUM(size), 0) FROM artifact WHERE session_id=?", (sid,)).fetchone()[0]
        if used + len(content) > MAX_SESSION_ARTIFACT_BYTES:
            raise ApiError(413, "payload_too_large", "This session has reached its 512 MiB artifact limit.")
        secret_guard.check(c, sid, name, media_type, description)
        secret_guard.check_bytes(c, sid, content)
        artifact_id, now = ids.new_ulid(), db.utcnow()
        data = {"artifact": artifact_id, "name": name, "media_type": media_type, "purpose": purpose,
                "size": len(content),
                "sha256": digest, **({"description": description} if description else {}),
                **({"task": f"T{num}"} if num else {})}
        seq, text = tx.event("artifact.added", task_num=num, data=data,
                             receipt=lambda event_seq: {"artifact": artifact_id, "session": s, "task": num,
                                                        "purpose": purpose, "size": len(content),
                                                        "sha256": digest, "seq": event_seq})
        c.execute("INSERT INTO artifact(session_id,id,task_num,name,media_type,description,size,sha256,content,"
                  "created_by,created_at,event_seq,purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, artifact_id, num, name, media_type, description, len(content), digest,
                   sqlite3.Binary(content), p.id, now, seq, purpose))
    return respond(text, 201)


@router.post("/sessions/{s}/artifacts", status_code=201)
async def upload_artifact(s: str, request: Request, name: str = Query("artifact.bin", max_length=255),
                          media_type: str = Query("application/octet-stream", max_length=127),
                          description: str | None = Query(None, max_length=2000),
                          task: str | None = Query(None, max_length=12),
                          purpose: str = Query("evidence", max_length=16),
                          key: str = Depends(idempotency_key),
                          recovery: str | None = Header(None, alias="X-Glassboard-Recovery"),
                          runtime: str | None = Depends(runtime_label),
                          p: tokens.Principal = Depends(require_full_scope),
                          database: db.Database = Depends(get_db)):
    return create_artifact(database, p, s, await request.body(), name=name, media_type=media_type,
                           description=description, task=task, purpose=purpose, key=key,
                           recovery=recovery, runtime=runtime)


@router.get("/sessions/{s}/artifacts")
def list_artifacts(s: str, task: str | None = Query(None, max_length=12),
                   p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        num = _task_num(c, sess["id"], task)
        rows = c.execute("SELECT * FROM artifact WHERE session_id=?" + (" AND task_num=?" if num is not None else "") +
                         " ORDER BY created_at, id", (sess["id"], num) if num is not None else (sess["id"],)).fetchall()
        return {"session": s, "artifacts": [artifact_view(c, row) for row in rows]}


def _load(conn, sid: str, artifact_id: str):
    row = conn.execute("SELECT * FROM artifact WHERE session_id=? AND id=?", (sid, artifact_id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such artifact in this session.")
    return row


@router.get("/sessions/{s}/artifacts/{artifact_id}")
def show_artifact(s: str, artifact_id: str, p: tokens.Principal = Depends(authenticate),
                  database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        return artifact_view(c, _load(c, sess["id"], artifact_id))


@router.get("/sessions/{s}/artifacts/{artifact_id}/content")
def download_artifact(s: str, artifact_id: str, p: tokens.Principal = Depends(authenticate),
                      database: db.Database = Depends(get_db)):
    with database.read() as c:
        sess, _ = require_member(c, s, p)
        row = _load(c, sess["id"], artifact_id)
        content, name, original_type = bytes(row["content"]), row["name"], row["media_type"]
    encoded = urllib.parse.quote(name, safe="")
    return Response(content, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
                             "X-Glassboard-Content-Type": original_type,
                             "X-Content-Type-Options": "nosniff"})
