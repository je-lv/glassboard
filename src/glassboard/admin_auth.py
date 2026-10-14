"""Password authentication for the local browser administrator."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from . import db

MIN_LENGTH = 16
MAX_LENGTH = 1024
_DUMMY_SALT = b"\0" * 16
_DUMMY_HASH = hashlib.scrypt(b"invalid-password", salt=_DUMMY_SALT,
                             n=1 << 14, r=8, p=1, dklen=32)


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=32)


def set_password(c, principal_id: str, password: str) -> None:
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise ValueError(f"admin password must be {MIN_LENGTH}–{MAX_LENGTH} characters")
    salt = secrets.token_bytes(16)
    c.execute("INSERT INTO admin_password(principal_id,salt,password_hash,changed_at) VALUES (?,?,?,?) "
              "ON CONFLICT(principal_id) DO UPDATE SET salt=excluded.salt, "
              "password_hash=excluded.password_hash, changed_at=excluded.changed_at",
              (principal_id, salt, _derive(password, salt), db.utcnow()))


def verify(c, handle: str, password: str):
    row = c.execute("SELECT p.id,p.handle,p.active,p.is_admin,a.salt,a.password_hash "
                    "FROM principal p JOIN admin_password a ON a.principal_id=p.id "
                    "WHERE p.handle=? AND p.kind='human'", (handle,)).fetchone()
    if not password or len(password) > MAX_LENGTH:
        return None
    eligible = row is not None and row["active"] and row["is_admin"]
    salt = row["salt"] if eligible else _DUMMY_SALT
    expected = row["password_hash"] if eligible else _DUMMY_HASH
    if not hmac.compare_digest(_derive(password, salt), expected) or not eligible:
        return None
    return row
