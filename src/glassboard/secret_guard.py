"""Secrets never enter the session log (protocol rule 10).

The log is append-only and readable by every member and every export, so a secret pasted into it
could never be taken back. Participant text is refused (`422 secret_in_content`) when it contains:
- any LIVE fence or consumer secret of that session (exact 10-character match; the server knows
  them, so there are no false positives from look-alike text such as "C2-compliance"), or
- anything shaped like a bearer token, `gbt_<10 base32>_<43 url-safe>` (only hashes are stored, so
  the value cannot be compared; the shape is specific enough).
The refused text is never echoed.
"""
from __future__ import annotations

import re
import sqlite3

from .errors import ApiError

TOKEN_RE = re.compile(r"gbt_[a-z2-7]{10}_[A-Za-z0-9_-]{43}")


def live_secrets(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute("SELECT consumer_secret FROM member WHERE session_id=? AND consumer_secret IS NOT NULL "
                        "UNION ALL SELECT fence_secret FROM task WHERE session_id=? AND fence_secret IS NOT NULL",
                        (session_id, session_id)).fetchall()
    return [r[0] for r in rows]


def check(conn: sqlite3.Connection, session_id: str | None, *texts) -> None:
    live = live_secrets(conn, session_id) if session_id else []
    for t in texts:
        if not isinstance(t, str) or not t:
            continue
        if TOKEN_RE.search(t) or any(s in t for s in live):
            raise ApiError(422, "secret_in_content", "This text contains a board secret (a token, fence or consumer token).",
                           hint="Remove it. Secrets must never enter the session log (protocol rule 10).")


def check_bytes(conn: sqlite3.Connection, session_id: str | None, content: bytes) -> None:
    """Refuse recognizable board credentials in an artifact without decoding or logging it."""
    live = live_secrets(conn, session_id) if session_id else []
    if re.search(rb"gbt_[a-z2-7]{10}_[A-Za-z0-9_-]{43}", content) or any(s.encode() in content for s in live):
        raise ApiError(422, "secret_in_content", "This artifact contains a board secret (a token, fence or consumer token).",
                       hint="Remove it. Secrets must never enter shared evidence.")


def strings(obj):
    """Every string inside a JSON-like value (for event data)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)
