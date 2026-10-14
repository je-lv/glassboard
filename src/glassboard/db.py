"""SQLite access.

Connection model:
- ONE writer connection per Database, serialised by a per-instance lock. `Database.write()` runs
  `BEGIN IMMEDIATE ... COMMIT` (ROLLBACK on any exception). All writes go through it, so the
  "single writer" rule holds inside the process; other processes (glassboard-admin) are
  serialised by SQLite's own locking plus busy_timeout.
- Readers: one connection per active outer read (reused by nested reads on that thread), opened
  with the same pragmas plus `query_only`, so a read path can never write by accident. Closing it
  after the outer read prevents idle worker threads from retaining SQLite file descriptors.
  `Database.read()` wraps the work in a deferred transaction: under WAL that is one consistent
  snapshot for all statements inside it.
- `:memory:` cannot be shared across connections, so there reads use the writer under its lock.

Migrations: numbered files `sql/NNN_name.sql`, applied in order, each in its own transaction
together with the `meta.schema_version` update. Statements are split with
`sqlite3.complete_statement`, so trigger bodies (`BEGIN ...; END;`) stay intact. Shipped
migration files are never edited; changes get a new file.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from typing import Iterator

from . import ids

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
# The first pre-release schema stored the string "b0a-1"; it corresponds to migration 001.
_LEGACY_VERSIONS = {"b0a-1": 1}


class SchemaTooNew(RuntimeError):
    """The database was migrated by a newer Glassboard; refuse to run against it."""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    """Opaque row ID. A ULID; see ids.py."""
    return ids.new_ulid()


def _configure(conn: sqlite3.Connection, path: str, query_only: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    if query_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


def connect(path: str, query_only: bool = False) -> sqlite3.Connection:
    """Open one connection with the standard pragmas. Autocommit mode: transactions are explicit."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread=False only because the writer is handed between threads under a lock.
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    return _configure(conn, path, query_only)


def split_statements(sql: str) -> list[str]:
    """Split a script into complete statements. Unlike `sql.split(";")` this keeps
    `CREATE TRIGGER ... BEGIN ...; END;` together."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt and not _only_comments(stmt):
                out.append(stmt)
            buf = ""
    if buf.strip() and not _only_comments(buf):
        raise ValueError("migration ends with an incomplete statement")
    return out


def _only_comments(s: str) -> bool:
    return all(not ln.strip() or ln.strip().startswith("--") for ln in s.splitlines()) or s.strip() == ";"


def migrations() -> list[tuple[int, str, str]]:
    """(number, filename, sql) for every packaged migration, ordered; numbers must be 1..N."""
    found = []
    for entry in resources.files("glassboard").joinpath("sql").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text()))
    found.sort()
    if [n for n, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migration numbers must be contiguous from 001")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        return 0
    v = row[0]
    if v in _LEGACY_VERSIONS:
        return _LEGACY_VERSIONS[v]
    return int(v)


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database to the latest schema. Idempotent; safe if several processes race
    (the version is re-read inside each BEGIN IMMEDIATE). Returns the resulting version."""
    known = migrations()
    latest = known[-1][0]
    for n, name, sql in known:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = schema_version(conn)
            if current > latest:
                raise SchemaTooNew(f"database schema {current} is newer than this build ({latest})")
            if current >= n:
                conn.execute("ROLLBACK")
                continue
            for stmt in split_statements(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO meta(key,value) VALUES ('schema_version', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(n),))
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('instance_id', ?)", (new_id(),))
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('created_at', ?)", (utcnow(),))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


_tx = threading.local()


def current_database() -> "Database | None":
    """The Database whose write transaction is open on this thread, if any."""
    return getattr(_tx, "database", None)


def on_commit(fn) -> None:
    """Run `fn` after the current write transaction commits (dropped on rollback). Outside a write
    transaction it runs at once."""
    hooks = getattr(_tx, "hooks", None)
    if hooks is None:
        fn()
    else:
        hooks.append(fn)


class Database:
    """One SQLite database file: a serialised writer plus per-thread snapshot readers."""

    def __init__(self, path: str):
        self.path = path
        self._writer = connect(path)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._closed = False
        # Called after a committed event: fn(session_id, seq, type, actor_id, recipients, broadcast).
        self.event_listeners: list = []

    # -- schema -------------------------------------------------------------------------------
    def migrate(self) -> int:
        with self._write_lock:
            return migrate(self._writer)

    # -- writes -------------------------------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` on the single writer; COMMIT on success, ROLLBACK on any exception.
        No network I/O may happen inside (hard rule 8). Callbacks registered with `on_commit()`
        during the transaction run after COMMIT, outside the lock; a rollback discards them."""
        hooks: list = []
        with self._write_lock:
            conn = self._writer
            conn.execute("BEGIN IMMEDIATE")
            _tx.hooks, _tx.database = hooks, self
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                hooks.clear()
                raise
            else:
                conn.execute("COMMIT")
            finally:
                _tx.hooks = _tx.database = None
        for fn in hooks:
            try:
                fn()
            except Exception:                 # a notification must never fail a committed write
                logging.getLogger("glassboard").exception("after-commit hook failed")

    # -- reads --------------------------------------------------------------------------------
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent snapshot: every statement inside sees the same committed state."""
        if self.path == ":memory:":
            with self._write_lock:
                yield self._writer
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:             # nested read on this thread: reuse the outer snapshot
            yield conn
            return
        if self._closed:
            raise RuntimeError("database is closed")
        conn = connect(self.path, query_only=True)
        self._local.conn = conn
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            self._local.conn = None
            conn.close()

    def close(self) -> None:
        self._closed = True
        with self._write_lock:
            self._writer.close()
