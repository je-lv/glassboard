"""security.journal: crash-safe record of every access reduction.

A backup can predate a revocation, a deactivation, a membership removal, a role downgrade, an
admin removal or an archive. Restoring it must not bring that access back. Every access-reducing
action is therefore appended here (fsynced) BEFORE its database change commits, and the whole
journal is re-applied at every startup and at restore. Only reductions are journaled, so a replay
can never grant anything.

File format: one JSON object per line, canonical (sorted keys, no spaces), UTF-8, "\n"-terminated:
    {"action": ..., <ids...>, "id": <ULID>, "n": 1, "op_id": ..., "prev": <hex>, "ts": ...}
`n` counts from 1 without gaps; `prev` is the SHA-256 (hex) of the previous line's bytes without
its newline; the first line's `prev` is 64 zeros. IDs only: never tokens, fences, consumer
tokens or free text.

Write path: the caller holds `BEGIN IMMEDIATE`, so appends are serialised across processes by
SQLite's write lock; `append()` writes with O_APPEND and fsyncs before returning; the caller then
applies the database change and commits. If the append fails, the caller's transaction rolls back
and nothing happened.

Startup (`replay`): inside BEGIN IMMEDIATE (so no live writer can be mid-append) the journal is
read and only entries newer than the database's watermark are applied; a torn LAST line (no trailing newline, or not valid JSON) is moved to
`security.journal.torn-<ts>` and the file truncated to the last good line; any other defect
(prev-hash break, gap in `n`, bad JSON before the tail, unknown action) refuses startup with the
line number. Then every entry is applied idempotently and monotonically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from . import db, ids

GENESIS = "0" * 64
ROLE_RANK = {"observer": 0, "member": 1, "coordinator": 2}
# action -> required id fields. Order matters only for documentation.
ACTIONS: dict[str, tuple[str, ...]] = {
    "credential.revoke": ("credential_id",),
    "principal.deactivate": ("principal_id",),
    "principal.admin_revoke": ("principal_id",),
    "member.remove": ("session_id", "principal_id"),
    "member.role_reduce": ("session_id", "principal_id", "role"),
    "session.archive": ("session_id",),
    "session.purge": ("session_id", "session_handle", "archive_id", "archive_sha256",
                      "archive_size", "head_seq", "epoch", "purged_by"),
    "web_session.revoke_all": ("principal_id",),
}

# Fault injection for crash tests. Test-only: set GLASSBOARD_TEST_FAULT=after_journal_append
# and the process dies immediately after the journal fsync, before the database commit. A crash at
# that point is fail-closed by design (the next startup applies the reduction), so the hook cannot
# grant access even if set by mistake; it can only make the process exit.
FAULT_ENV = "GLASSBOARD_TEST_FAULT"


class JournalError(RuntimeError):
    """The journal cannot be trusted; the service must not start (operator decides)."""


class JournalWriteError(RuntimeError):
    """Appending failed; the caller must roll back (HTTP 503 `unavailable`).

    `uncertain` is False when the file was restored to its pre-append length, so the reduction
    did not happen. It is True when that undo itself failed: the entry may still be in the file
    and would then be applied at the next startup (fail closed)."""

    def __init__(self, message: str, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def default_path(db_path: str) -> str:
    """`GLASSBOARD_JOURNAL`, else `security.journal` next to the database file."""
    env = os.environ.get("GLASSBOARD_JOURNAL")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "security.journal")


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def _validate_entry(e: object, lineno: int) -> dict:
    if not isinstance(e, dict):
        raise JournalError(f"security.journal line {lineno}: not an object")
    action = e.get("action")
    if action not in ACTIONS:
        raise JournalError(f"security.journal line {lineno}: unknown action")
    need = {"n", "id", "ts", "action", "op_id", "prev", *ACTIONS[action]}
    if set(e) != need:
        raise JournalError(f"security.journal line {lineno}: unexpected or missing fields")
    if action == "member.role_reduce" and e["role"] not in ROLE_RANK:
        raise JournalError(f"security.journal line {lineno}: invalid role")
    if action == "session.purge":
        if not re.fullmatch(r"[0-9a-f]{64}", e["archive_sha256"]) or any(
                not isinstance(e[name], str) or not e[name].isdigit() or int(e[name]) < 1
                for name in ("archive_size", "head_seq", "epoch")):
            raise JournalError(f"security.journal line {lineno}: invalid purge receipt")
    return e


def read_entries(path: str) -> tuple[list[dict], bytes | None, int, str]:
    """Parse and verify the journal. Returns (entries, torn_tail_bytes_or_None, good_byte_length,
    hash_of_last_good_line). Raises JournalError on any defect other than a torn last line."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return [], None, 0, GENESIS
    lines = data.split(b"\n")
    # data ending in "\n" gives a final empty element; anything else in the last slot is a tail
    # without newline, i.e. torn.
    complete, tail = lines[:-1], lines[-1]
    torn: bytes | None = tail if tail else None
    entries: list[dict] = []
    prev, good_len = GENESIS, 0
    for i, raw in enumerate(complete, start=1):
        is_last_complete = i == len(complete) and torn is None
        try:
            obj = json.loads(raw)
        except ValueError:
            if is_last_complete:          # a newline-terminated but unparseable last line is torn too
                torn = raw + b"\n"
                break
            raise JournalError(f"security.journal line {i}: invalid JSON (corruption)") from None
        e = _validate_entry(obj, i)
        if e["prev"] != prev:
            raise JournalError(f"security.journal line {i}: prev-hash chain broken (corruption)")
        if e["n"] != i:
            raise JournalError(f"security.journal line {i}: sequence number {e['n']} out of order")
        entries.append(e)
        prev = _hash(raw)
        good_len += len(raw) + 1
    return entries, torn, good_len, prev


def _fsync_dir(path: str) -> None:
    fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(path: str, action: str, op_id: str | None = None, **fields: str) -> dict:
    """Append one entry and fsync it. The caller MUST hold the database write transaction
    (BEGIN IMMEDIATE) and apply + commit the database change only after this returns."""
    if action not in ACTIONS or set(fields) != set(ACTIONS[action]):
        raise ValueError("journal entry fields do not match the action")
    try:
        entries, torn, _, prev = read_entries(path)
        if torn is not None:
            # Only startup may quarantine a torn tail; refusing here keeps the chain honest.
            raise JournalWriteError("security.journal has a torn tail; restart the service")
        entry = {"n": len(entries) + 1, "id": ids.new_ulid(), "ts": db.utcnow(), "action": action,
                 "op_id": op_id or ids.new_ulid(), "prev": prev, **fields}
        line = _canonical(entry) + b"\n"
        created = not os.path.exists(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        orig_size = os.fstat(fd).st_size      # we hold the DB write lock: no other appender
        try:
            os.fchmod(fd, 0o600)
            written = os.write(fd, line)
            if written != len(line):
                raise OSError("short write to security.journal")
            os.fsync(fd)
            if created:
                _fsync_dir(path)
        except OSError as e:
            # write() may have succeeded even though fsync() failed. Undo the append so that
            # "nothing changed" is true; if the undo fails too, say the outcome is uncertain.
            try:
                os.ftruncate(fd, orig_size)
            except OSError:
                raise JournalWriteError(f"{e}; undo failed, the reduction may take effect at the "
                                        "next startup (fail closed)", uncertain=True) from e
            try:
                os.fsync(fd)
            except OSError:
                pass        # the truncated file is what every later reader sees
            raise JournalWriteError(str(e)) from e
        finally:
            os.close(fd)
    except JournalWriteError:
        raise
    except (OSError, JournalError) as e:
        raise JournalWriteError(str(e)) from e
    if os.environ.get(FAULT_ENV) == "after_journal_append":
        os._exit(70)   # simulated crash: journal durable, database change never committed
    return entry


WATERMARK = "journal_applied_n"


def watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (WATERMARK,)).fetchone()
    return int(row[0]) if row else 0


def _advance(conn: sqlite3.Connection, n: int) -> None:
    """The database records the newest journal entry it has applied, in the same
    transaction as the change."""
    conn.execute("INSERT INTO meta(key,value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = CAST(MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT)", (WATERMARK, str(n)))


def apply_entry(conn: sqlite3.Connection, e: dict) -> bool:
    """Apply one reduction; never grants. Returns True if anything changed. Always advances the
    database's journal watermark to this entry."""
    try:
        return _apply(conn, e)
    finally:
        _advance(conn, e["n"])


def _apply(conn: sqlite3.Connection, e: dict) -> bool:
    a, now = e["action"], e["ts"]
    if a == "credential.revoke":
        cur = conn.execute("UPDATE credential SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                           (now, e["credential_id"]))
    elif a == "principal.deactivate":
        cur = conn.execute("UPDATE principal SET active=0 WHERE id=? AND active=1", (e["principal_id"],))
    elif a == "principal.admin_revoke":
        cur = conn.execute("UPDATE principal SET is_admin=0 WHERE id=? AND is_admin=1", (e["principal_id"],))
    elif a == "member.remove":
        cur = conn.execute("UPDATE member SET left_at=? WHERE session_id=? AND principal_id=? AND left_at IS NULL",
                           (now, e["session_id"], e["principal_id"]))
    elif a == "member.role_reduce":
        row = conn.execute("SELECT role FROM member WHERE session_id=? AND principal_id=?",
                           (e["session_id"], e["principal_id"])).fetchone()
        if row is None or ROLE_RANK[row["role"]] <= ROLE_RANK[e["role"]]:
            return False
        cur = conn.execute("UPDATE member SET role=? WHERE session_id=? AND principal_id=?",
                           (e["role"], e["session_id"], e["principal_id"]))
    elif a == "session.archive":
        cur = conn.execute("UPDATE session SET state='archived', updated_at=? WHERE id=? AND state!='archived'",
                           (db.utcnow(), e["session_id"]))
    elif a == "session.purge":
        row = conn.execute("SELECT handle FROM session WHERE id=?", (e["session_id"],)).fetchone()
        if row is not None and row["handle"] != e["session_handle"]:
            raise JournalError("session purge journal entry does not match the stored workspace")
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO workspace_purge(id,session_id,session_handle,epoch,head_seq,archive_id,"
            "archive_sha256,archive_size,purged_by,purged_at,journal_n) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e["id"], e["session_id"], e["session_handle"], int(e["epoch"]), int(e["head_seq"]),
             e["archive_id"], e["archive_sha256"], int(e["archive_size"]), e["purged_by"], e["ts"], e["n"]),
        )
        if row is not None:
            conn.execute("INSERT INTO workspace_purge_guard(session_id) VALUES (?)", (e["session_id"],))
            try:
                # The core schema intentionally has no cascading deletes. Keep the order explicit:
                # artifacts reference tasks and events; recipients reference events.
                conn.execute("DELETE FROM artifact WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM event_recipient WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM checkpoint WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM lease WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM invite WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM member WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM task WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM event WHERE session_id=?", (e["session_id"],))
                conn.execute("DELETE FROM session WHERE id=?", (e["session_id"],))
            finally:
                conn.execute("DELETE FROM workspace_purge_guard WHERE session_id=?", (e["session_id"],))
        return conn.total_changes > before
    elif a == "web_session.revoke_all":
        cur = conn.execute("UPDATE web_session SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
                           (now, e["principal_id"]))
    else:  # pragma: no cover - read_entries already rejects unknown actions
        raise JournalError("unknown action")
    return cur.rowcount > 0


def _make_visible(conn: sqlite3.Connection, e: dict, role_before: str | None) -> None:
    """A replayed session-scoped reduction commits one board-authored event."""
    from . import events   # local import: events → db; journal stays importable on its own
    a, n = e["action"], e["n"]
    if a in ("member.remove", "member.role_reduce"):
        h = conn.execute("SELECT handle FROM principal WHERE id=?", (e["principal_id"],)).fetchone()
        handle = h["handle"] if h else None
        if a == "member.remove":
            conn.execute("UPDATE member SET consumer_secret=NULL, consumer_runtime=NULL, consumer_since=NULL "
                         "WHERE session_id=? AND principal_id=?", (e["session_id"], e["principal_id"]))
            events.append(conn, e["session_id"], ids.BOARD_PRINCIPAL_ID, "member.removed",
                          recipients=[e["principal_id"]], data={"handle": handle, "journal_n": n})
        else:
            events.append(conn, e["session_id"], ids.BOARD_PRINCIPAL_ID, "member.changed",
                          recipients=[e["principal_id"]],
                          data={"handle": handle, "from": role_before, "to": e["role"], "journal_n": n})
    elif a == "session.archive":
        events.append(conn, e["session_id"], ids.BOARD_PRINCIPAL_ID, "session.archived", broadcast=True,
                      data={"journal_n": n})


def replay(database: "db.Database", path: str, emit_events: bool = True) -> list[int]:
    """Startup/restore: quarantine a torn tail, verify the chain, apply every entry.
    Returns the `n` of entries that changed something. Raises JournalError on corruption.
    `emit_events=False` is for restore, where one `system.restored` event per session covers
    every change."""
    with database.write() as conn:
        entries, torn, good_len, _ = read_entries(path)
        if torn is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            qpath = f"{path}.torn-{stamp}"
            fd = os.open(qpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, torn)
                os.fsync(fd)
            finally:
                os.close(fd)
            with open(path, "r+b") as f:
                f.truncate(good_len)
                f.flush()
                os.fsync(f.fileno())
            _fsync_dir(path)
        # only entries newer than the database's watermark are applied. Older ones are
        # already in this database, and any later grant (a re-added member, a new web session, an
        # unarchive) must not be undone by a restart. A restored backup carries its own older
        # watermark, so every reduction made after the backup is applied again.
        mark = watermark(conn)
        if mark > len(entries):
            raise JournalError(f"security.journal has {len(entries)} entries but the database has applied "
                               f"{mark}: the journal was truncated or replaced")
        changed = []
        for e in entries[mark:]:
            before = None
            if e["action"] == "member.role_reduce":
                r = conn.execute("SELECT role FROM member WHERE session_id=? AND principal_id=?",
                                 (e["session_id"], e["principal_id"])).fetchone()
                before = r["role"] if r else None
            if apply_entry(conn, e):
                changed.append(e["n"])
                if emit_events:
                    _make_visible(conn, e, before)
        if changed or torn is not None:
            # Glass box for instance-level state: one audit row when replay did anything.
            conn.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                         (ids.new_ulid(), db.utcnow(), ids.BOARD_PRINCIPAL_ID, "journal.replay", None,
                          json.dumps({"applied": changed, "torn_tail_quarantined": torn is not None})))
        return changed
