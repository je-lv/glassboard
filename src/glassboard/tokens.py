"""Credential format and verification (gbt_<id>_<secret>)."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_B32 = "abcdefghijklmnopqrstuvwxyz234567"
TOKEN_RE = re.compile(r"^gbt_([a-z2-7]{10})_([A-Za-z0-9_-]{43})$")
CRED_ID_RE = re.compile(r"^[a-z2-7]{10}$")                 # the public credential id inside a token
HANDLE_SEG_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_DUMMY_HASH = hashlib.sha256(b"glassboard-dummy").digest()


def new_token() -> tuple[str, str, bytes]:
    """Return (token, credential_id, secret_sha256). The token is shown once and never stored."""
    cid = "".join(secrets.choice(_B32) for _ in range(10))
    secret = secrets.token_urlsafe(32)  # 43 chars, 256 bits
    return f"gbt_{cid}_{secret}", cid, hashlib.sha256(secret.encode()).digest()


@dataclass(frozen=True)
class Principal:
    id: str
    handle: str
    kind: str
    owner_handle: str | None
    is_admin: bool
    credential_id: str
    scope: str
    expires_at: str | None
    last_used_at: str | None = None


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def verify(conn: sqlite3.Connection, token: str) -> Principal | None:
    m = TOKEN_RE.match(token)
    if not m:
        hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
        return None
    cid, secret = m.groups()
    row = conn.execute(
        """SELECT c.id cid, c.secret_sha256, c.scope, c.expires_at, c.revoked_at, c.last_used_at,
                  p.id pid, p.handle, p.kind, p.is_admin, p.active, o.handle owner_handle, o.active owner_active
           FROM credential c JOIN principal p ON p.id = c.principal_id
           LEFT JOIN principal o ON o.id = p.owner_id
           WHERE c.id = ?""",
        (cid,),
    ).fetchone()
    presented = hashlib.sha256(secret.encode()).digest()
    stored = row["secret_sha256"] if row else _DUMMY_HASH
    ok = hmac.compare_digest(presented, stored)
    if not row or not ok:
        return None
    if row["revoked_at"] or not row["active"] or (row["owner_handle"] is not None and not row["owner_active"]):
        return None
    if row["kind"] == "system":   # the `board` principal authors events but never authenticates
        return None
    if row["expires_at"] and _parse_ts(row["expires_at"]) <= datetime.now(timezone.utc):
        return None
    return Principal(
        id=row["pid"], handle=row["handle"], kind=row["kind"], owner_handle=row["owner_handle"],
        is_admin=bool(row["is_admin"]), credential_id=row["cid"], scope=row["scope"],
        expires_at=row["expires_at"], last_used_at=row["last_used_at"],
    )
