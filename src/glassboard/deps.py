"""Request dependencies shared by every router: database handle and bearer authentication."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request

from . import db, tokens
from .errors import ApiError

# credential.last_used_at is bookkeeping. Writing it on every request
# would take the global write lock for every read, so it is refreshed at most once per minute.
LAST_USED_RESOLUTION = timedelta(seconds=60)
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="glassboard"'}


def get_db(request: Request) -> db.Database:
    return request.app.state.db


def client_addr(request: Request) -> str:
    """The client address used for per-IP limits. Behind Caddy this is the real LAN address only
    when uvicorn trusts X-Forwarded-For from 127.0.0.1 (the deploy unit and verify
    script pass --proxy-headers --forwarded-allow-ips 127.0.0.1)."""
    return request.client.host if request.client else "unknown"


def _stale(last_used_at: str | None) -> bool:
    if not last_used_at:
        return True
    t = datetime.strptime(last_used_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - t >= LAST_USED_RESOLUTION


def authenticate(request: Request, database: db.Database = Depends(get_db)) -> tokens.Principal:
    limiter = request.app.state.auth_limiter
    addr = client_addr(request)
    wait = limiter.retry_after(addr)
    if wait is not None:
        # Checked before the token: while limited, even a correct guess learns nothing.
        raise ApiError(429, "rate_limited", "Too many authentication failures from this address.",
                       hint="Wait for Retry-After seconds.", retryable=True,
                       headers={"Retry-After": str(wait)})
    h = request.headers.get("authorization", "")
    scheme, _, tok = h.partition(" ")
    if scheme.lower() != "bearer" or not tok:
        limiter.record_failure(addr)
        raise ApiError(401, "unauthenticated", "Missing bearer token.", headers=_CHALLENGE)
    with database.read() as conn:
        p = tokens.verify(conn, tok.strip())
    if p is None:
        limiter.record_failure(addr)
        raise ApiError(401, "unauthenticated", "Invalid, expired or revoked token.", headers=_CHALLENGE)
    request.state.principal_id = p.id
    if _stale(p.last_used_at):
        with database.write() as c:   # bookkeeping, no event
            c.execute("UPDATE credential SET last_used_at=? WHERE id=?", (db.utcnow(), p.credential_id))
    return p


def require_full_scope(p: tokens.Principal = Depends(authenticate)) -> tokens.Principal:
    """Mutations need a `full` token; `read` tokens may only read (credential.scope)."""
    if p.scope != "full":
        raise ApiError(403, "forbidden", "This token is read-only.")
    return p
