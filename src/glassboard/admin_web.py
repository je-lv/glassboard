"""Browser instance administration. Host-only operations stay outside a live HTTP request."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import admin_auth, db, ids, journal, tokens
from .errors import ApiError
from .sessions import journal_error


def _audit(c, actor_id: str, action: str, target: str | None, data: dict | None = None) -> None:
    c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
              (ids.new_ulid(), db.utcnow(), actor_id, action, target, json.dumps(data or {})))


def _target(c, handle: str):
    row = c.execute("SELECT * FROM principal WHERE handle=? AND kind!='system'", (handle,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such person or agent.")
    return row


def _seg(value: str) -> str:
    if not tokens.HANDLE_SEG_RE.fullmatch(value):
        raise ApiError(422, "invalid_request", "Use 1–32 lowercase letters, numbers or hyphens.")
    return value


def create_principal(database: db.Database, actor, *, kind: str, name: str, owner: str = "",
                     display_name: str = "", admin: bool = False) -> str:
    if kind not in ("human", "agent", "client"):
        raise ApiError(422, "invalid_request", "Invalid identity kind.")
    name = _seg(name.strip())
    if len(display_name) > 64:
        raise ApiError(422, "invalid_request", "Display name is too long.")
    with database.write() as c:
        owner_id = None
        if kind != "human":
            human = c.execute("SELECT id FROM principal WHERE handle=? AND kind='human' AND active=1", (owner,)).fetchone()
            if human is None:
                raise ApiError(422, "invalid_request", "Choose an active human owner.")
            owner_id = human["id"]
        handle = f"{owner}/{name}" if owner_id else name
        if c.execute("SELECT 1 FROM principal WHERE handle=?", (handle,)).fetchone():
            raise ApiError(409, "handle_taken", "That handle already exists.")
        pid = ids.new_ulid()
        c.execute("INSERT INTO principal(id,handle,kind,owner_id,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                  (pid, handle, kind, owner_id, display_name.strip() or handle, int(admin and kind == "human"), db.utcnow()))
        _audit(c, actor.id, "principal.create", pid, {"handle": handle, "kind": kind, "is_admin": bool(admin and kind == "human")})
        return handle


def change_access(database: db.Database, actor, jpath: str, *, action: str, handle: str) -> None:
    if action not in ("grant-admin", "revoke-admin", "deactivate", "revoke-sessions"):
        raise ApiError(422, "invalid_request", "Unknown access action.")
    try:
        with database.write() as c:
            target = _target(c, handle)
            if action in ("grant-admin", "revoke-admin") and target["kind"] != "human":
                raise ApiError(422, "invalid_request", "Only humans can be admins.")
            if action == "grant-admin":
                if not target["active"]:
                    raise ApiError(409, "inactive", "Activate the human first.")
                if not target["is_admin"]:
                    # A previous admin password must never revive when access is granted again.
                    c.execute("DELETE FROM admin_password WHERE principal_id=?", (target["id"],))
                    c.execute("UPDATE principal SET is_admin=1 WHERE id=?", (target["id"],))
                    _audit(c, actor.id, "principal.admin_grant", target["id"], {"handle": handle})
                return
            if action in ("revoke-admin", "deactivate") and target["id"] == actor.id:
                raise ApiError(409, "self_access", "You cannot remove your own admin access here.")
            if action in ("revoke-admin", "deactivate") and target["is_admin"] and target["active"]:
                count = c.execute("SELECT count(*) FROM principal WHERE kind='human' AND is_admin=1 AND active=1").fetchone()[0]
                if count <= 1:
                    raise ApiError(409, "last_admin", "At least one active admin must remain.")
            if action == "revoke-admin":
                if target["is_admin"]:
                    entry = journal.append(jpath, "principal.admin_revoke", principal_id=target["id"])
                    journal.apply_entry(c, entry)
                    _audit(c, actor.id, "principal.admin_revoke", target["id"], {"handle": handle, "journal_n": entry["n"]})
            elif action == "deactivate":
                if target["active"]:
                    entry = journal.append(jpath, "principal.deactivate", principal_id=target["id"])
                    journal.apply_entry(c, entry)
                    _audit(c, actor.id, "principal.deactivate", target["id"], {"handle": handle, "journal_n": entry["n"]})
            else:
                live = c.execute("SELECT count(*) FROM web_session WHERE principal_id=? AND revoked_at IS NULL AND expires_at>?",
                                 (target["id"], db.utcnow())).fetchone()[0]
                if live:
                    entry = journal.append(jpath, "web_session.revoke_all", principal_id=target["id"])
                    journal.apply_entry(c, entry)
                    _audit(c, actor.id, "web_session.revoke_all", target["id"], {"handle": handle, "sessions": live, "journal_n": entry["n"]})
    except journal.JournalWriteError as exc:
        raise journal_error(exc) from None


def reset_password(database: db.Database, actor, *, handle: str, current: str, new: str) -> None:
    with database.write() as c:
        if admin_auth.verify(c, actor.handle, current) is None:
            raise ApiError(403, "wrong_password", "Your current password was not accepted.")
        target = _target(c, handle)
        if target["kind"] != "human" or not target["active"] or not target["is_admin"]:
            raise ApiError(422, "invalid_request", "Password login requires an active human admin.")
        try:
            admin_auth.set_password(c, target["id"], new)
        except ValueError:
            raise ApiError(422, "invalid_password", "Use a password of 16–1024 characters.") from None
        _audit(c, actor.id, "admin_password.set", target["id"], {"handle": handle})


def mint_token(database: db.Database, actor, *, handle: str, scope: str, days: int | None, label: str) -> dict:
    if scope not in ("full", "read") or (days is not None and not 1 <= days <= 3650) or len(label) > 64:
        raise ApiError(422, "invalid_request", "Check the token scope, expiry and label.")
    with database.write() as c:
        target = _target(c, handle)
        if not target["active"]:
            raise ApiError(409, "inactive", "That identity is inactive.")
        secret, cid, digest = tokens.new_token()
        expires = ((datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                   if days is not None else None)
        c.execute("INSERT INTO credential(id,principal_id,secret_sha256,scope,label,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                  (cid, target["id"], digest, scope, label.strip() or None, db.utcnow(), expires))
        _audit(c, actor.id, "credential.create", cid, {"principal_id": target["id"], "scope": scope})
        return {"token": secret, "id": cid, "principal": handle, "scope": scope, "expires": expires}


def revoke_token(database: db.Database, actor, jpath: str, credential_id: str) -> None:
    try:
        with database.write() as c:
            row = c.execute("SELECT id,revoked_at FROM credential WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                raise ApiError(404, "not_found", "No such token.")
            if row["revoked_at"]:
                return
            entry = journal.append(jpath, "credential.revoke", credential_id=credential_id)
            journal.apply_entry(c, entry)
            _audit(c, actor.id, "credential.revoke", credential_id, {"journal_n": entry["n"]})
    except journal.JournalWriteError as exc:
        raise journal_error(exc) from None
