"""Admin CLI, credentials, security journal."""
import glob
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from glassboard import admin, db, ids, journal, tokens
from glassboard.app import create_app


def auth(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def inst(tmp_path, capsys):
    dbp = str(tmp_path / "b.db")
    jp = str(tmp_path / "security.journal")
    def run(*args):
        rc = admin.main(["--db", dbp, *args])
        return rc, capsys.readouterr()
    run("add-human", "jose", "--admin")
    run("add-agent", "jose", "claude")
    def mint(handle="jose/claude"):
        return run("mint-token", handle)[1].out.strip()
    def app():
        return TestClient(create_app(dbp, allowed_hosts=["testserver"]), raise_server_exceptions=False)
    return {"db": dbp, "journal": jp, "run": run, "mint": mint, "app": app, "tmp": tmp_path}


def lines(path):
    with open(path, "rb") as f:
        return f.read().split(b"\n")[:-1]


# --- journaled before the response; crash between journal and commit ---------------------------

def test_at_e4_1_journal_fsynced_before_commit(inst, monkeypatch):
    tok = inst["mint"]()
    cid = tok.split("_")[1]
    order = []
    real_fsync = os.fsync
    def rec_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)
    monkeypatch.setattr(journal.os, "fsync", rec_fsync)
    real_write = db.Database.write
    from contextlib import contextmanager
    @contextmanager
    def rec_write(self):
        with real_write(self) as c:
            yield c
            order.append("commit-pending")      # body finished; COMMIT happens right after this
        order.append("committed")
    monkeypatch.setattr(db.Database, "write", rec_write)
    rc, out = inst["run"]("revoke-token", cid)
    assert rc == 0 and out.out.strip() == "revoked"
    # The journal fsync happened while the revoke transaction was still open: the next
    # transaction marker after it is that transaction's own pre-COMMIT point, not a finished commit.
    i_fsync = order.index("fsync")
    after = [x for x in order[i_fsync + 1:] if x != "fsync"]
    assert after[:2] == ["commit-pending", "committed"]
    e = json.loads(lines(inst["journal"])[-1])
    assert e["action"] == "credential.revoke" and e["credential_id"] == cid


@pytest.mark.parametrize("cmd", ["revoke-token", "deactivate"])
def test_at_e4_1_crash_after_journal_before_commit(inst, cmd):
    """Fault injection: the admin process dies right after the journal fsync. The database change
    never committed, yet after restart the reduction is in force (fail closed)."""
    tok = inst["mint"]()
    cid = tok.split("_")[1]
    target = cid if cmd == "revoke-token" else "jose/claude"
    env = {**os.environ, journal.FAULT_ENV: "after_journal_append"}
    r = subprocess.run([sys.executable, "-m", "glassboard.admin", "--db", inst["db"], cmd, target],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 70, r.stderr                    # crashed at the injected point
    raw = sqlite3.connect(inst["db"])
    if cmd == "revoke-token":                               # database really did not commit
        assert raw.execute("SELECT revoked_at FROM credential WHERE id=?", (cid,)).fetchone()[0] is None
    else:
        assert raw.execute("SELECT active FROM principal WHERE handle='jose/claude'").fetchone()[0] == 1
    raw.close()
    client = inst["app"]()                                  # restart: replay applies the entry
    assert client.get("/api/v1/whoami", headers=auth(tok)).status_code == 401
    raw = sqlite3.connect(inst["db"])
    a = raw.execute("SELECT actor_id, data FROM audit WHERE action='journal.replay'").fetchone()
    assert a[0] == ids.BOARD_PRINCIPAL_ID and json.loads(a[1])["applied"] == [1]


def test_at_e12_1_fsync_failure_undoes_append(inst, monkeypatch):
    """write() succeeded but fsync() failed. The append is undone, so the reduction
    really did not happen, now or at the next startup."""
    inst["run"]("deactivate", "jose")            # make sure the journal exists with one entry
    before = open(inst["journal"], "rb").read()
    tok = inst["mint"]()
    cid = tok.split("_")[1]
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(journal.os, "fsync", boom)
    rc, out = inst["run"]("revoke-token", cid)
    assert rc == 4 and "nothing changed" in out.err
    monkeypatch.undo()
    assert open(inst["journal"], "rb").read() == before
    raw = sqlite3.connect(inst["db"])
    assert raw.execute("SELECT revoked_at FROM credential WHERE id=?", (cid,)).fetchone()[0] is None
    inst["app"]()                                # restart: nothing to apply for this credential
    assert raw.execute("SELECT revoked_at FROM credential WHERE id=?", (cid,)).fetchone()[0] is None


def test_at_e12_2_undo_failure_reports_uncertain_and_fails_closed(inst, monkeypatch):
    tok = inst["mint"]()
    cid = tok.split("_")[1]
    def boom(*a, **k):
        raise OSError("I/O error")
    monkeypatch.setattr(journal.os, "fsync", boom)
    monkeypatch.setattr(journal.os, "ftruncate", boom)
    rc, out = inst["run"]("revoke-token", cid)
    assert rc == 4 and "outcome uncertain" in out.err and "nothing changed" not in out.err
    monkeypatch.undo()
    # The entry stayed in the file, so the next startup applies it: fail closed.
    assert inst["app"]().get("/api/v1/whoami", headers=auth(tok)).status_code == 401


# --- torn tail ----------------------------------------------------------------------------------

@pytest.mark.parametrize("tail", [b'{"action":"credential.revoke","credential_id":"x', b"not json at all\n"])
def test_at_e4_2_torn_tail_quarantined(inst, tail):
    tok = inst["mint"]()
    cid = tok.split("_")[1]
    inst["run"]("deactivate", "jose/claude")     # one good entry
    good = open(inst["journal"], "rb").read()
    with open(inst["journal"], "ab") as f:
        f.write(tail)
    client = inst["app"]()                       # startup succeeds
    assert client.get("/health/live").status_code == 200
    q = glob.glob(inst["journal"] + ".torn-*")
    assert len(q) == 1 and open(q[0], "rb").read() == tail
    assert open(inst["journal"], "rb").read() == good          # truncated to the last good line
    # The torn entry was not applied: the credential itself is not revoked.
    raw = sqlite3.connect(inst["db"])
    assert raw.execute("SELECT revoked_at FROM credential WHERE id=?", (cid,)).fetchone()[0] is None
    # and the journal is writable again, chain intact
    rc, _ = inst["run"]("revoke-token", cid)
    assert rc == 0 and len(lines(inst["journal"])) == 2
    journal.read_entries(inst["journal"])


# --- corruption before the tail refuses startup ------------------------------------------------

def test_at_e4_3_mid_file_prev_mismatch_refuses_start(inst, capsys):
    a, b = inst["mint"](), inst["mint"]()
    inst["run"]("revoke-token", a.split("_")[1])
    inst["run"]("revoke-token", b.split("_")[1])
    ls = lines(inst["journal"])
    e1 = json.loads(ls[0]); e1["ts"] = "2000-01-01T00:00:00.000000Z"     # tamper line 1
    with open(inst["journal"], "wb") as f:
        f.write(json.dumps(e1, sort_keys=True, separators=(",", ":")).encode() + b"\n" + ls[1] + b"\n")
    with pytest.raises(journal.JournalError, match="line 2: prev-hash chain broken"):
        create_app(inst["db"], allowed_hosts=["testserver"])
    rc = admin.main(["--db", inst["db"], "add-human", "ana"])
    assert rc == 3 and "line 2" in capsys.readouterr().err
    assert sqlite3.connect(inst["db"]).execute("SELECT count(*) FROM principal WHERE handle='ana'").fetchone()[0] == 0


@pytest.mark.parametrize("mutate,msg", [
    (lambda e: {**e, "n": 5}, "line 1: sequence number"),
    (lambda e: {**e, "action": "credential.grant"}, "line 1: unknown action"),
    (lambda e: {**e, "extra": "x"}, "line 1: unexpected or missing fields"),
])
def test_invalid_entries_refuse_start(inst, mutate, msg):
    inst["run"]("revoke-token", inst["mint"]().split("_")[1])
    inst["run"]("revoke-token", inst["mint"]().split("_")[1])
    ls = lines(inst["journal"])
    bad = json.dumps(mutate(json.loads(ls[0])), sort_keys=True, separators=(",", ":")).encode()
    with open(inst["journal"], "wb") as f:
        f.write(bad + b"\n" + ls[1] + b"\n")
    with pytest.raises(journal.JournalError, match=msg):
        journal.read_entries(inst["journal"])


# --- the journal holds IDs only ---------------------------------------------------------------

def test_at_e4_6_journal_contains_no_secrets(inst):
    toks = [inst["mint"]() for _ in range(3)]
    for t in toks:
        inst["run"]("revoke-token", t.split("_")[1])
    inst["run"]("deactivate", "jose/claude")
    inst["run"]("revoke-admin", "jose")
    data = open(inst["journal"], "rb").read()
    for t in toks:
        assert t.split("_", 2)[2].encode() not in data and t.encode() not in data
    for raw in lines(inst["journal"]):
        e = json.loads(raw)
        assert set(e) == {"n", "id", "ts", "action", "op_id", "prev", *journal.ACTIONS[e["action"]]}
    assert stat.S_IMODE(os.stat(inst["journal"]).st_mode) == 0o600


# --- restore cannot resurrect access (the purpose of the security journal) --------------------

def test_old_backup_does_not_resurrect_revoked_token(inst):
    tok = inst["mint"]()
    backup = str(inst["tmp"] / "backup.db")
    shutil.copy(inst["db"], backup)               # quiescent copy: the admin process has exited
    inst["run"]("revoke-token", tok.split("_")[1])
    inst["run"]("deactivate", "jose")
    os.replace(backup, inst["db"])                # "restore" the pre-revoke database
    for suffix in ("-wal", "-shm"):
        if os.path.exists(inst["db"] + suffix):
            os.remove(inst["db"] + suffix)
    client = inst["app"]()
    assert client.get("/api/v1/whoami", headers=auth(tok)).status_code == 401
    raw = sqlite3.connect(inst["db"])
    assert raw.execute("SELECT active FROM principal WHERE handle='jose'").fetchone()[0] == 0


# --- admin CLI paths ----------------------------------------------------------------------------

def test_revoke_and_deactivate_go_through_journal(inst):
    tok = inst["mint"]()
    assert inst["run"]("revoke-token", tok.split("_")[1])[1].out.strip() == "revoked"
    assert inst["run"]("revoke-token", tok.split("_")[1])[1].out.strip() == "already revoked or unknown"
    assert inst["run"]("deactivate", "jose/claude")[1].out.strip() == "deactivated"
    assert inst["run"]("deactivate", "jose/claude")[1].out.strip() == "already inactive"
    assert inst["run"]("revoke-admin", "jose")[1].out.strip() == "admin revoked"
    assert inst["run"]("revoke-admin", "jose")[1].out.strip() == "not an admin"
    actions = [json.loads(l)["action"] for l in lines(inst["journal"])]
    assert actions == ["credential.revoke", "principal.deactivate", "principal.admin_revoke"]  # no-ops not journaled
    entries, torn, _, _ = journal.read_entries(inst["journal"])
    assert torn is None and [e["n"] for e in entries] == [1, 2, 3]


def test_audit_actor_is_board_with_os_user(inst):
    raw = sqlite3.connect(inst["db"])
    rows = raw.execute("SELECT actor_id, data FROM audit").fetchall()
    assert rows and all(r[0] == ids.BOARD_PRINCIPAL_ID for r in rows)
    assert all(json.loads(r[1])["os_user"] for r in rows)
    assert raw.execute("SELECT count(*) FROM audit WHERE actor_id='local-admin'").fetchone()[0] == 0


def test_system_principal_not_changeable(inst):
    for cmd in ("deactivate", "revoke-admin", "mint-token"):
        with pytest.raises(SystemExit, match="system principals"):
            inst["run"](cmd, "board")


# --- apply rules: monotone reductions, never grants --------------------------------------------

def _entry(n, action, **f):
    return {"n": n, "id": ids.new_ulid(), "ts": db.utcnow(), "action": action, "op_id": ids.new_ulid(),
            "prev": "", **f}


def test_apply_rules_are_monotone_reductions(tmp_path):
    d = db.Database(str(tmp_path / "m.db"))
    d.migrate()
    with d.write() as c:
        c.execute("INSERT INTO principal(id,handle,kind,display_name,is_admin,created_at) VALUES ('h','h','human','h',1,'x')")
        c.execute("INSERT INTO session(id,handle,title,state,recovery_id,protocol_version,created_at,updated_at)"
                  " VALUES ('S','s','t','active','R','1','x','x')")
        c.execute("INSERT INTO member(session_id,principal_id,role,joined_seq,cursor_seq,delivered_seq,joined_at)"
                  " VALUES ('S','h','coordinator',0,0,0,'x')")
        c.execute("INSERT INTO web_session(id_sha256,principal_id,csrf_token,created_at,expires_at) VALUES (x'01','h','c','x','y')")
        role = lambda: c.execute("SELECT role FROM member").fetchone()[0]
        A = journal.apply_entry
        assert A(c, _entry(1, "member.role_reduce", session_id="S", principal_id="h", role="member")) and role() == "member"
        # A journaled "coordinator" after a reduction to member never promotes back (min rule).
        assert not A(c, _entry(2, "member.role_reduce", session_id="S", principal_id="h", role="coordinator"))
        assert role() == "member"
        assert A(c, _entry(3, "member.role_reduce", session_id="S", principal_id="h", role="observer")) and role() == "observer"
        assert A(c, _entry(4, "member.remove", session_id="S", principal_id="h"))
        assert not A(c, _entry(5, "member.remove", session_id="S", principal_id="h"))           # idempotent
        assert A(c, _entry(6, "session.archive", session_id="S"))
        assert c.execute("SELECT state FROM session").fetchone()[0] == "archived"
        assert not A(c, _entry(7, "session.archive", session_id="S"))
        assert A(c, _entry(8, "web_session.revoke_all", principal_id="h"))
        assert c.execute("SELECT revoked_at FROM web_session").fetchone()[0]
        assert A(c, _entry(9, "principal.admin_revoke", principal_id="h"))
        assert A(c, _entry(10, "principal.deactivate", principal_id="h"))
        assert not A(c, _entry(11, "principal.deactivate", principal_id="h"))
        # Unknown targets are harmless no-ops (e.g. rows lost with an older backup).
        assert not A(c, _entry(12, "credential.revoke", credential_id="nope"))
    d.close()


def test_append_rejects_mismatched_fields(tmp_path):
    with pytest.raises(ValueError):
        journal.append(str(tmp_path / "j"), "credential.revoke", principal_id="x")
    with pytest.raises(ValueError):
        journal.append(str(tmp_path / "j"), "credential.grant", credential_id="x")


def test_concurrent_appends_from_two_processes_keep_the_chain(inst):
    """Two admin processes append at the same time; SQLite's write lock serialises them."""
    toks = [inst["mint"]() for _ in range(16)]
    cids = [t.split("_")[1] for t in toks]
    script = ("import sys\nfrom glassboard import admin\n"
              "for cid in sys.argv[2:]:\n"
              "    assert admin.main(['--db', sys.argv[1], 'revoke-token', cid]) == 0\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, inst["db"], *cids[i::2]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) for i in range(2)]
    for p in procs:
        assert p.wait(timeout=120) == 0, p.stderr.read()
    entries, torn, _, _ = journal.read_entries(inst["journal"])
    assert torn is None and [e["n"] for e in entries] == list(range(1, 17))
    assert sorted(e["credential_id"] for e in entries) == sorted(cids)
    raw = sqlite3.connect(inst["db"])
    assert raw.execute("SELECT count(*) FROM credential WHERE revoked_at IS NOT NULL").fetchone()[0] == 16
