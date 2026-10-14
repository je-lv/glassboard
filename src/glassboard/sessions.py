"""Session and membership helpers shared by routers and the mutation pipeline.

Routers import this module; it never imports a router.
"""
from __future__ import annotations

import re
import sqlite3

from . import journal, tokens
from .errors import ApiError

# Session handles start with a letter, so they can never collide with a ULID (uppercase).
SESSION_HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PRINCIPAL_HANDLE_RE = re.compile(r"^[a-z0-9-]{1,32}(/[a-z0-9-]{1,32})?$")
ROLE_RANK = journal.ROLE_RANK
RECOVERY_ID_CHARS = 16          # 80 bits


def find_session(conn: sqlite3.Connection, s: str):
    return conn.execute("SELECT * FROM session WHERE handle=? OR id=?", (s, s)).fetchone()


def active_member(conn: sqlite3.Connection, session_id: str, principal_id: str):
    return conn.execute("SELECT * FROM member WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                        (session_id, principal_id)).fetchone()


def require_member(conn: sqlite3.Connection, s: str, p: tokens.Principal):
    """404 for unknown sessions AND for sessions the caller is not an active member of."""
    sess = find_session(conn, s)
    me = active_member(conn, sess["id"], p.id) if sess else None
    if sess is None or me is None:
        raise ApiError(404, "not_found", "Not found.")
    return sess, me


def check_recovery(sess, presented: str | None) -> None:
    """Every session mutation carries the session's current recovery_id."""
    if not presented:
        raise ApiError(428, "recovery_required", "This request needs the X-Glassboard-Recovery header.",
                       hint="Read the session's recovery_id from GET /api/v1/sessions.")
    if presented != sess["recovery_id"]:
        raise ApiError(409, "recovery_mismatch", "The board was restored; this recovery ID is stale.",
                       hint="Stop writing and run `glassboard reconcile`.", details={"epoch": sess["epoch"]})


def require_human_coordinator(p: tokens.Principal, me) -> None:
    if p.kind != "human" or me["role"] != "coordinator":
        raise ApiError(403, "forbidden", "Only a human coordinator may do this.")


def journal_error(e: journal.JournalWriteError) -> ApiError:
    if e.uncertain:
        return ApiError(503, "unavailable", "Could not record the change; outcome uncertain.",
                        hint="The change may take effect after a restart. Check before retrying.", retryable=True)
    return ApiError(503, "unavailable", "Could not record the change; nothing changed.", retryable=True)


def session_view(sess, role: str) -> dict:
    return {"session": sess["handle"], "id": sess["id"], "title": sess["title"], "state": sess["state"],
            "role": role, "recovery_id": sess["recovery_id"], "epoch": sess["epoch"], "head": sess["head_seq"],
            "autonomous": bool(sess["autonomous"])}


def my_sessions(conn: sqlite3.Connection, principal_id: str) -> list[dict]:
    rows = conn.execute("SELECT s.*, m.role FROM member m JOIN session s ON s.id = m.session_id "
                        "WHERE m.principal_id=? AND m.left_at IS NULL ORDER BY s.handle", (principal_id,))
    return [session_view(r, r["role"]) for r in rows]


def _human_coordinators(conn, session_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT m.principal_id FROM member m JOIN principal p ON p.id = m.principal_id "
        "WHERE m.session_id=? AND m.role='coordinator' AND m.left_at IS NULL AND p.kind='human' AND p.active=1",
        (session_id,))]


# --- pause / archive ------------------------------------------------------------------------------
# Which operations each session state allows. Fixed: paused → claim, note, reclaim refused;
# checkpoint, release, post, sync allowed. Also fixed: archived blocks participant mutations, while
# board-authored events still happen. The other cells are: pause stops progress on claimed work
# (note, block, unblock, claim, reclaim) while governance and handing work back go on; archive
# freezes everything except access reductions, reading and acking.
STATES = ("active", "paused", "archived")
ALLOWED: dict[str, frozenset[str]] = {
    "message": frozenset({"active", "paused"}),
    "resolve": frozenset({"active", "paused"}),
    "sync.attach": frozenset({"active", "paused"}),          # attach and takeover write events
    "sync.ack": frozenset({"active", "paused", "archived"}),  # bookkeeping only
    "checkpoint": frozenset({"active", "paused"}),
    "task.create": frozenset({"active", "paused"}),
    "task.claim": frozenset({"active"}),
    "task.acknowledge": frozenset({"active"}),
    "task.progress": frozenset({"active"}),                  # note, block, unblock
    "task.reclaim": frozenset({"active"}),                   # reclaim needs an active session
    "task.finish": frozenset({"active", "paused"}),          # submit, release
    "task.coord": frozenset({"active", "paused"}),           # PATCH, accept, return, reassign, cancel, reopen
    "member.add": frozenset({"active", "paused"}),           # also raising a role
    "member.reduce": frozenset({"active", "paused", "archived"}),   # removals and role reductions
}
TRANSITIONS = {("active", "paused"): "session.paused", ("paused", "active"): "session.resumed",
               ("active", "archived"): "session.archived", ("paused", "archived"): "session.archived",
               ("archived", "active"): "session.resumed"}


def require_state(sess, op: str) -> None:
    """409 session_paused / session_archived when the session's state does not allow `op`."""
    if sess["state"] in ALLOWED[op]:
        return
    if sess["state"] == "paused":
        raise ApiError(409, "session_paused", "The session is paused.", hint="A coordinator can resume it.")
    raise ApiError(409, "session_archived", "The session is archived.", hint="A coordinator can unarchive it.")
