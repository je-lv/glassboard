"""Schema, migrations, connections."""
import sqlite3
import threading
import time

import pytest

from glassboard import admin, db, ids, tokens

LATEST = db.migrations()[-1][0]
TABLES = {"meta", "principal", "credential", "audit", "web_session", "session", "member", "event",
          "event_recipient", "task", "checkpoint"}


def schema_snapshot(path):
    c = sqlite3.connect(path)
    rows = c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    c.close()
    return rows


def seed_session(conn, sid="S1"):
    conn.execute("INSERT INTO session(id,handle,title,state,recovery_id,protocol_version,created_at,updated_at)"
                 " VALUES (?,?,?,?,?,?,?,?)", (sid, "s-" + sid.lower(), "t", "active", "R", "1", db.utcnow(), db.utcnow()))


def seed_event(conn, sid="S1", seq=1, receipt='{"ok":1}'):
    conn.execute("INSERT INTO event(session_id,seq,id,recovery_id,epoch,ts,actor_id,type,receipt)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (sid, seq, ids.new_ulid(), "R", 1, db.utcnow(), ids.BOARD_PRINCIPAL_ID, "note", receipt))


@pytest.fixture
def database(tmp_path):
    d = db.Database(str(tmp_path / "b.db"))
    d.migrate()
    yield d
    d.close()


# --- the event log is append-only ----------------------------------------------------------------

def test_at_4_4_receipt_update_aborted_by_trigger(database):
    with database.write() as c:
        seed_session(c)
        seed_event(c)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.write() as c:
            c.execute("UPDATE event SET receipt='{\"forged\":1}' WHERE session_id='S1' AND seq=1")
    with database.read() as r:
        assert r.execute("SELECT receipt FROM event").fetchone()[0] == '{"ok":1}'


def test_at_4_4_delete_aborted_by_trigger(database):
    with database.write() as c:
        seed_session(c)
        seed_event(c)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.write() as c:
            c.execute("DELETE FROM event")
    with database.read() as r:
        assert r.execute("SELECT count(*) FROM event").fetchone()[0] == 1


# --- migrations ----------------------------------------------------------------------------------

def test_migrate_idempotent_on_empty_db(tmp_path):
    p = str(tmp_path / "e.db")
    d = db.Database(p)
    assert d.migrate() == LATEST
    first = schema_snapshot(p)
    assert d.migrate() == LATEST           # second run is a no-op
    d.close()
    d2 = db.Database(p)                     # and so is a fresh process-like open
    assert d2.migrate() == LATEST
    d2.close()
    assert schema_snapshot(p) == first
    names = {n for t, n, _ in first if t == "table"}
    assert TABLES <= names


def test_migrate_idempotent_on_current_b0a_db(tmp_path):
    """A database from the early prototype (schema_version 'b0a-1') upgrades in place, data kept."""
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p, isolation_level=None)
    c.executescript(db.migrations()[0][2])
    c.execute("INSERT INTO meta VALUES ('schema_version','b0a-1')")
    c.execute("INSERT INTO principal(id,handle,kind,display_name,created_at) VALUES ('h1','jose','human','jose','x')")
    c.close()
    d = db.Database(p)
    assert d.migrate() == LATEST
    assert d.migrate() == LATEST
    with d.read() as r:
        assert r.execute("SELECT handle FROM principal WHERE id='h1'").fetchone()[0] == "jose"
        assert r.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(LATEST)
    d.close()
    assert TABLES <= {n for t, n, _ in schema_snapshot(p) if t == "table"}


def test_concurrent_migrators_converge(tmp_path):
    """Two processes (here: two Database objects in threads) migrating the same file at once."""
    p = str(tmp_path / "race.db")
    errors, dbs = [], [db.Database(p) for _ in range(4)]
    def go(d):
        try:
            d.migrate()
        except Exception as e:  # pragma: no cover - the assertion below reports it
            errors.append(e)
    ts = [threading.Thread(target=go, args=(d,)) for d in dbs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    [d.close() for d in dbs]
    assert errors == []
    c = sqlite3.connect(p)
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(LATEST)
    assert c.execute("SELECT count(*) FROM principal WHERE handle='board'").fetchone()[0] == 1


def test_schema_newer_than_build_refused(tmp_path):
    p = str(tmp_path / "new.db")
    d = db.Database(p)
    d.migrate()
    with d.write() as c:
        c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(LATEST + 1),))
    with pytest.raises(db.SchemaTooNew):
        d.migrate()
    d.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    p = str(tmp_path / "f.db")
    good = db.migrations()
    broken = good + [(LATEST + 1, "999_broken.sql", "CREATE TABLE half_done (x);\nTHIS IS NOT SQL;\n")]
    monkeypatch.setattr(db, "migrations", lambda: broken)
    d = db.Database(p)
    with pytest.raises(sqlite3.OperationalError):
        d.migrate()
    with d.read() as r:
        assert r.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(LATEST)
        assert r.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone() is None
    d.close()


def test_split_statements_keeps_trigger_bodies():
    sql = ("-- header comment\nCREATE TABLE a (x);\n"
           "CREATE TRIGGER t BEFORE DELETE ON a BEGIN SELECT RAISE(ABORT,'no; really'); SELECT 1; END;\n"
           "INSERT INTO a VALUES ('semi;colon');\n-- trailing comment\n")
    stmts = db.split_statements(sql)
    assert len(stmts) == 3
    assert stmts[1].startswith("CREATE TRIGGER") and stmts[1].rstrip().endswith("END;")
    assert "'semi;colon'" in stmts[2]
    with pytest.raises(ValueError):
        db.split_statements("CREATE TABLE b (x)")   # no terminating semicolon


def test_full_v04_ddl_and_errata_columns(database):
    with database.read() as r:
        cols = {row["name"]: row for row in r.execute("PRAGMA table_info(checkpoint)")}
        assert cols["recovery_id"]["notnull"] == 1
        member = {row["name"] for row in r.execute("PRAGMA table_info(member)")}
        assert {"delivered_seq", "consumer_gen", "consumer_secret", "consumer_runtime", "consumer_since"} <= member
        trig = {row[0] for row in r.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert trig == {"event_immutable_u", "event_immutable_d", "artifact_immutable_update", "artifact_immutable_delete",
                        "role_template_revision_immutable_u", "role_template_revision_immutable_d",
                        "workspace_archive_immutable_u", "workspace_archive_immutable_d",
                        "workspace_purge_immutable_u", "workspace_purge_immutable_d",
                        "session_reject_purged_handle"}
        b = r.execute("SELECT id, kind, owner_id FROM principal WHERE handle='board'").fetchone()
        assert (b["id"], b["kind"], b["owner_id"]) == (ids.BOARD_PRINCIPAL_ID, "system", None)


def test_member_cursor_never_above_delivered(database):
    with database.write() as c:
        seed_session(c)
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as c:
            c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                      " VALUES ('S1',?, 'member', 0, 5, 4, 'x')", (ids.BOARD_PRINCIPAL_ID,))


def test_foreign_keys_enforced(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as c:
            c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                      " VALUES ('nope','nope','member',0,0,0,'x')")


# --- connection model -------------------------------------------------------------

def _pragmas(conn):
    return (conn.execute("PRAGMA foreign_keys").fetchone()[0], conn.execute("PRAGMA journal_mode").fetchone()[0],
            conn.execute("PRAGMA synchronous").fetchone()[0], conn.execute("PRAGMA busy_timeout").fetchone()[0])


def test_every_connection_has_the_pragmas(database):
    with database.write() as w:
        assert _pragmas(w) == (1, "wal", 2, 5000)
    with database.read() as r:
        assert _pragmas(r) == (1, "wal", 2, 5000)


def test_readers_are_per_active_read_and_query_only(database):
    seen = {}
    def grab(name):
        with database.read() as r:
            seen[name] = r
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                r.execute("INSERT INTO meta VALUES ('x','y')")
    ts = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(seen.values())) == 3
    for r in seen.values():
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            r.execute("SELECT 1")
    with database.read() as a, database.read() as b:   # same thread → same reader connection
        assert a is b
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        a.execute("SELECT 1")


def test_read_is_a_consistent_snapshot(database):
    with database.write() as c:
        seed_session(c)
    with database.read() as r:
        before = r.execute("SELECT count(*) FROM session").fetchone()[0]
        t = threading.Thread(target=lambda: _write_session(database, "S2"))
        t.start(); t.join()
        assert r.execute("SELECT count(*) FROM session").fetchone()[0] == before   # same snapshot
    with database.read() as r:
        assert r.execute("SELECT count(*) FROM session").fetchone()[0] == before + 1


def _write_session(database, sid):
    with database.write() as c:
        seed_session(c, sid)


def test_concurrent_writes_serialise_without_lost_updates(database):
    with database.write() as c:
        c.execute("INSERT INTO meta VALUES ('counter','0')")
    n_threads, n_each, errors = 8, 25, []
    def worker():
        try:
            for _ in range(n_each):
                with database.write() as c:
                    v = int(c.execute("SELECT value FROM meta WHERE key='counter'").fetchone()[0])
                    time.sleep(0)            # invite a context switch inside the transaction
                    c.execute("UPDATE meta SET value=? WHERE key='counter'", (str(v + 1),))
                with database.read() as r:
                    r.execute("SELECT count(*) FROM principal").fetchone()
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    with database.read() as r:
        assert r.execute("SELECT value FROM meta WHERE key='counter'").fetchone()[0] == str(n_threads * n_each)


def test_write_rolls_back_on_exception(database):
    with pytest.raises(RuntimeError):
        with database.write() as c:
            seed_session(c)
            raise RuntimeError("boom")
    with database.read() as r:
        assert r.execute("SELECT count(*) FROM session").fetchone()[0] == 0
    with database.write() as c:           # the writer is usable again (lock released, no open tx)
        seed_session(c)


# --- IDs --------------------------------------------------------------------------

def test_ulids_are_well_formed_unique_and_time_ordered():
    a = ids.new_ulid(ms=1_700_000_000_000)
    b = ids.new_ulid(ms=1_700_000_000_001)
    assert ids.ULID_RE.match(a) and ids.ULID_RE.match(b) and a < b
    assert ids.ulid_ms(a) == 1_700_000_000_000
    many = {ids.new_ulid() for _ in range(5000)}
    assert len(many) == 5000
    assert ids.ULID_RE.match(db.new_id())
    assert ids.ULID_RE.match(ids.BOARD_PRINCIPAL_ID) and ids.ulid_ms(ids.BOARD_PRINCIPAL_ID) == 0
    assert ids.new_ulid(ms=(1 << 48) - 1)[0] == "7"
    with pytest.raises(ValueError):
        ids.new_ulid(ms=1 << 48)


def test_admin_rows_get_ulids(tmp_path, capsys):
    p = str(tmp_path / "a.db")
    admin.main(["--db", p, "add-human", "jose"])
    c = sqlite3.connect(p)
    pid = c.execute("SELECT id FROM principal WHERE handle='jose'").fetchone()[0]
    assert ids.ULID_RE.match(pid)


# --- the board system principal never authenticates --------------------------------------------

def test_board_principal_cannot_hold_or_use_credentials(tmp_path, capsys):
    p = str(tmp_path / "s.db")
    admin.main(["--db", p, "init"])
    with pytest.raises(SystemExit, match="system principals"):
        admin.main(["--db", p, "mint-token", "board"])
    # Defence in depth: even a credential row inserted behind the CLI's back is not accepted.
    tok, cid, digest = tokens.new_token()
    d = db.Database(p)
    with d.write() as c:
        c.execute("INSERT INTO credential(id,principal_id,secret_sha256,scope,created_at) VALUES (?,?,?,?,?)",
                  (cid, ids.BOARD_PRINCIPAL_ID, digest, "full", db.utcnow()))
    with d.read() as r:
        assert tokens.verify(r, tok) is None
    d.close()
