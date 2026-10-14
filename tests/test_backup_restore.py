"""Backup and restore with recovery IDs (old recovery IDs and fences refused after a restore, revocations
survive it, events after the backup are gone and redelivered, removed access never comes back, archived
sessions on restore, pre-restore checkpoints included after a real restore; encryption; host loss)."""
import json
import os
import shutil
import sqlite3
import stat
import sys

import pytest

from glassboard import admin_auth, backup as backup_mod
from glassboard import ids
from test_render import assert_canonical

FAKE_AGE = os.path.join(os.path.dirname(__file__), "fake_age.py")


def admin(gb, *args):
    return gb.admin(*args)


def take(gb, name="b1.db", *flags):
    out = str(gb.tmp / name)
    rc, o = admin(gb, "backup", out, *(flags or ("--no-encrypt",)))
    assert rc == 0, o.err
    return out


def restore(gb, path, *flags):
    gb.stop()                                               # restore needs the service stopped
    gb.raw.close()
    rc, o = admin(gb, "restore", path, *flags)
    gb.raw = sqlite3.connect(gb.db, isolation_level=None)
    gb.raw.row_factory = sqlite3.Row
    from fastapi.testclient import TestClient
    from glassboard.app import create_app
    gb.app = create_app(gb.db, allowed_hosts=["testserver"])
    gb.client = TestClient(gb.app, raise_server_exceptions=False)
    return rc, o


def api(gb, method, path, who, body=None, key=True, rec=None):
    return gb.req(method, f"/api/v1/sessions/s1{path}", who, json=body if body is not None else {},
                  rec=rec or gb.rec(), key=key)


@pytest.fixture
def r(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    gb.add("s1", "jose/claude")
    return gb


# --- backup ---------------------------------------------------------------------------------------

def test_backup_needs_encryption_or_an_explicit_opt_out(r):
    rc, o = admin(r, "backup", str(r.tmp / "x.db"))
    assert rc == 5 and "--age-recipient" in o.err and not os.path.exists(r.tmp / "x.db")


def test_plaintext_backup_is_consistent_private_and_has_a_journal_copy(r):
    api(r, "POST", "/messages", "jose", {"kind": "note", "body": "in the WAL"})   # the app still has it open
    r.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=r.rec())  # one journal entry
    out = take(r)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
    assert meta["backup_taken_at"] and json.loads(meta["backup_head_seqs"])
    assert c.execute("SELECT count(*) FROM event WHERE body='in the WAL'").fetchone()[0] == 1   # online backup API
    assert open(out + ".journal").read() == open(r.app.state.journal_path).read()
    assert r.raw.execute("SELECT count(*) FROM audit WHERE action='backup'").fetchone()[0] == 1


def test_restore_refused_while_the_board_runs(r):
    out = take(r)
    rc, o = admin(r, "restore", out)
    assert rc == 5 and "stop the service" in o.err


# --- recovery identifier -------------------------------------------------------------------------

def test_at_3_1_two_restores_two_recovery_ids(r):
    out = take(r)
    rec0, ep0 = r.rec(), r.raw.execute("SELECT epoch FROM session").fetchone()[0]
    restore(r, out)
    rec1, ep1 = r.rec(), r.raw.execute("SELECT epoch FROM session").fetchone()[0]
    restore(r, out)
    rec2, ep2 = r.rec(), r.raw.execute("SELECT epoch FROM session").fetchone()[0]
    assert len({rec0, rec1, rec2}) == 3 and (ep1, ep2) == (ep0 + 1, ep0 + 1)     # each from the same backup: +1
    assert len(rec1) == 16


def test_at_3_2_and_3_4_old_recovery_id_is_refused_after_restore(r):
    tok = api(r, "POST", "/sync", "jose/claude").json()["consumer"]
    out = take(r)
    rec0 = r.rec()
    restore(r, out)
    head, cursor = r.head(), r.raw.execute("SELECT cursor_seq FROM member WHERE principal_id=?", (r.pid("jose/claude"),)).fetchone()[0]
    m = api(r, "POST", "/messages", "jose", {"kind": "note", "body": "x"}, rec=rec0)
    assert m.status_code == 409 and m.json()["error"]["code"] == "recovery_mismatch" and r.head() == head
    s = api(r, "POST", "/sync", "jose/claude", {"consumer": tok, "ack_through": 3}, key=None, rec=rec0)
    assert s.status_code == 409 and s.json()["error"]["code"] == "recovery_mismatch"
    assert r.raw.execute("SELECT cursor_seq FROM member WHERE principal_id=?", (r.pid("jose/claude"),)).fetchone()[0] == cursor


def test_at_3_5_pre_restore_fence_is_dead(r):
    api(r, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"})
    fence = api(r, "POST", "/tasks/1/claim", "jose/claude").json()["fence"]
    out = take(r)
    restore(r, out)
    api(r, "POST", "/state", "jose", {"state": "active", "reason": "reconciled"})
    t = r.raw.execute("SELECT status, gen, fence_secret, claimant_id FROM task").fetchone()
    assert (t["status"], t["gen"], t["fence_secret"], t["claimant_id"]) == ("in_progress", 2, None, r.pid("jose/claude"))
    x = api(r, "POST", "/tasks/1/note", "jose/claude", {"fence": fence, "note": "x"})
    assert x.status_code == 409 and x.json()["error"]["code"] == "fence_mismatch"


def test_at_3_7_revocation_after_the_backup_survives_the_restore(r):
    tok = r.mint("jose/claude")
    out = take(r)
    admin(r, "revoke-token", tok.split("_")[1])
    restore(r, out)
    assert r.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_at_3_8_after_restore(r):
    from test_dashboard import login
    r.session("s2", who="ana")
    api(r, "POST", "/sync", "jose/claude")
    login(r)
    out = take(r)
    taken = dict(sqlite3.connect(out).execute("SELECT key, value FROM meta").fetchall())["backup_taken_at"]
    rc, o = restore(r, out)
    assert rc == 0 and "Sessions are paused" in o.out
    assert {x[0] for x in r.raw.execute("SELECT state FROM session")} == {"paused"}
    for h in ("s1", "s2"):
        ev = [e for e in r.events(h) if e["type"] == "system.restored"]
        assert len(ev) == 1 and ev[0]["actor_id"] == ids.BOARD_PRINCIPAL_ID and ev[0]["broadcast"] == 1
        assert ev[0]["data"]["backup_taken_at"] == taken and ev[0]["data"]["backup_head_seq"] == ev[0]["seq"] - 1
    assert r.raw.execute("SELECT count(*) FROM member WHERE consumer_secret IS NOT NULL").fetchone()[0] == 0
    assert r.raw.execute("SELECT count(*) FROM web_session WHERE revoked_at IS NULL").fetchone()[0] == 0
    last = json.loads(r.raw.execute("SELECT value FROM meta WHERE key='last_restore'").fetchone()[0])
    assert last["backup_taken_at"] == taken and last["sessions"] == 2
    t = r.req("GET", "/api/v1/sessions/s1/events?type=system.restored&format=text", "jose", key=None).text
    assert f"  · backup taken: «{taken}»" in t and "Session paused until a coordinator resumes." in t
    assert_canonical(t)


def test_restore_requires_admin_password_rebootstrap(r):
    r.raw.execute("UPDATE principal SET is_admin=1 WHERE handle='jose'")
    admin_auth.set_password(r.raw, r.pid("jose"), "a long password before restore")
    out = take(r)
    admin_auth.set_password(r.raw, r.pid("jose"), "a different password after backup")
    assert restore(r, out)[0] == 0
    assert r.raw.execute("SELECT count(*) FROM admin_password").fetchone()[0] == 0


def test_at_3_10_reclaim_while_paused_then_after_resume(r):
    api(r, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"})
    api(r, "POST", "/tasks/1/claim", "jose/claude")
    restore(r, take(r))
    x = api(r, "POST", "/tasks/1/reclaim", "jose/claude", {"reason": "after restore"})
    assert x.status_code == 409 and x.json()["error"]["code"] == "session_paused"
    assert api(r, "POST", "/state", "ana", {"state": "active", "reason": "reconciled"}).status_code == 200   # any coordinator
    y = api(r, "POST", "/tasks/1/reclaim", "jose/claude", {"reason": "after restore"}).json()
    assert y["gen"] == 3 and y["fence"].startswith("T1g3-")


# --- events lost by a restore, pre-restore checkpoints ----------------------------------------------

def test_at_8_12_everything_after_the_backup_is_gone_and_redelivered(r):
    api(r, "POST", "/tasks", "jose", {"title": "t", "scope": "s", "deliverable": "d"})
    tok = api(r, "POST", "/sync", "jose/claude").json()["consumer"]
    before_seq = api(r, "POST", "/messages", "jose", {"kind": "note", "body": "before", "to": ["jose/claude"]}).json()["seq"]
    out = take(r)
    for i in range(5):
        api(r, "POST", "/messages", "jose", {"kind": "note", "body": f"lost {i}", "to": ["jose/claude"]})
    api(r, "POST", "/tasks/1/claim", "jose/claude")
    api(r, "POST", "/checkpoints", "jose/claude", {"summary": "lost", "next_action": "n"})
    s = api(r, "POST", "/sync", "jose/claude", {"consumer": tok}, key=None).json()
    api(r, "POST", "/sync", "jose/claude", {"consumer": tok, "ack_through": s["delivered_through"]}, key=None)
    restore(r, out)
    bodies = [e["body"] for e in r.events()]
    assert not any((b or "").startswith("lost") for b in bodies)
    assert r.raw.execute("SELECT status FROM task").fetchone()[0] == "open"
    assert r.raw.execute("SELECT count(*) FROM checkpoint").fetchone()[0] == 0
    api(r, "POST", "/state", "jose", {"state": "active", "reason": "go"})
    again = api(r, "POST", "/sync", "jose/claude").json()                 # re-attach: consumer was cleared
    assert before_seq in [e["seq"] for e in again["inbox"]]               # the regressed cursor redelivers
    assert any(e["type"] == "system.restored" for e in again["inbox"])


def test_e3_checkpoint_from_before_a_real_restore_is_included(r):
    api(r, "POST", "/checkpoints", "jose/claude", {"summary": "before the restore", "next_action": "carry on"})
    out = take(r)
    restore(r, out)
    j = api(r, "POST", "/sync", "jose/claude").json()
    assert j["checkpoint"]["summary"] == "before the restore" and j["checkpoint"]["recovery_id"] != r.rec()


# --- removed access never comes back; archived sessions ------------------------------------------------

def test_at_e4_4_no_access_comes_back(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    gb.add("s1", "ana/codex")
    gb.session("s2")
    out = take(gb)
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana/codex", "jose", rec=gb.rec())       # remove member M
    gb.add("s1", "ana", role="member")                                                     # downgrade coordinator C
    gb.req("POST", "/api/v1/sessions/s2/state", "jose", json={"state": "archived", "reason": "done"}, rec=gb.rec("s2"))
    gb.admin("deactivate", "jose")                                                          # deactivate human H
    restore(gb, out)
    assert gb.req("GET", "/api/v1/sessions/s1/members", "ana/codex", key=None).status_code == 404
    assert gb.req("GET", "/api/v1/whoami", "jose", key=None).status_code == 401
    assert gb.req("GET", "/api/v1/whoami", "jose/claude", key=None).status_code == 401    # H's agents too
    assert gb.raw.execute("SELECT role FROM member WHERE principal_id=?", (gb.pid("ana"),)).fetchone()[0] == "member"
    assert gb.raw.execute("SELECT state FROM session WHERE handle='s2'").fetchone()[0] == "archived"


def test_at_e4_5_remove_then_readd_ends_removed(r):
    out = take(r)
    r.req("DELETE", "/api/v1/sessions/s1/members/jose/claude", "jose", rec=r.rec())
    r.add("s1", "jose/claude")
    restore(r, out)
    assert r.req("GET", "/api/v1/sessions/s1/members", "jose/claude", key=None).status_code == 404


def test_at_e5b_1_archived_sessions_on_restore(gb):
    gb.session("a")
    gb.session("b")
    gb.req("POST", "/api/v1/sessions/b/state", "jose", json={"state": "archived", "reason": "old"}, rec=gb.rec("b"))
    out = take(gb)
    gb.req("POST", "/api/v1/sessions/a/state", "jose", json={"state": "archived", "reason": "after backup"}, rec=gb.rec("a"))
    recs = {h: gb.rec(h) for h in ("a", "b")}
    restore(gb, out)
    for h in ("a", "b"):
        assert gb.raw.execute("SELECT state FROM session WHERE handle=?", (h,)).fetchone()[0] == "archived"
        assert gb.rec(h) != recs[h]
        ev = [e for e in gb.events(h) if e["type"] == "system.restored"]
        assert len(ev) == 1 and "stays archived" in ev[0]["body"]


# --- CLI quarantine and reconcile end to end -------------------------------------------------------

def test_at_3_6_cli_quarantine_and_reconcile_after_a_real_restore(r, live_server):
    L = live_server
    assert L.cli("jose", "use", "s1").returncode == 0
    assert L.cli("jose", "post", "note", "before the backup").returncode == 0
    committed = r.events()[-1]
    out = take(r)
    L.stop()
    for msg in ("queued A", "queued B"):
        assert L.cli("jose", "post", "note", msg).returncode == 4                     # offline: queued under rec0
    # the response to "before the backup" was lost too: put it back in the outbox, as a client would have it
    state_home = L.env("jose")["XDG_STATE_HOME"]
    from glassboard import clistate
    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = state_home
    try:
        st = clistate.SessionState("s1")
        st.add_outbox({"op": committed["op_id"], "method": "POST", "path": "/api/v1/sessions/s1/messages",
                       "body": {"kind": "note", "body": "before the backup"}, "recovery_id": committed["recovery_id"],
                       "created": "x", "what": "post note", "fenced": False})
    finally:
        os.environ.pop("XDG_STATE_HOME") if old is None else os.environ.__setitem__("XDG_STATE_HOME", old)
    rc, o = restore(r, out)
    assert rc == 0
    L.start()
    head = r.head()
    res = L.cli("jose", "post", "note", "after the restore")
    assert res.returncode == 7 and r.head() == head                                    # nothing sent under rec0
    for args in (("sync",), ("post", "note", "again")):
        assert L.cli("jose", *args).returncode == 7
    assert r.head() == head
    out_r = L.cli("jose", "reconcile").stdout
    assert "adopted recovery" in out_r and "quarantine: 4 operation(s)" in out_r and "[system.restored]" in out_r
    assert L.cli("jose", "session", "resume", "reconciled").returncode == 0
    head = r.head()
    resend = L.cli("jose", "reconcile", "--resend", committed["op_id"])
    assert resend.returncode == 0 and f"#{committed['seq']}" in resend.stdout and r.head() == head   # original receipt
    os.environ["XDG_STATE_HOME"] = state_home
    try:
        op_a = next(e["op"] for e in clistate.SessionState("s1").quarantine() if e["body"]["body"] == "queued A")
    finally:
        os.environ.pop("XDG_STATE_HOME") if old is None else os.environ.__setitem__("XDG_STATE_HOME", old)
    assert L.cli("jose", "reconcile", "--resend", op_a).returncode == 0
    assert r.events()[-1]["body"] == "queued A" and r.head() == head + 1                # lost op applied once
    assert L.cli("jose", "reconcile", "--resend", op_a).returncode == 2                  # no longer quarantined


# --- encryption, host loss, bad input ------------------------------------------------------------

def test_encrypted_backup_and_restore(r, monkeypatch, tmp_path):
    monkeypatch.setenv("GLASSBOARD_AGE", FAKE_AGE)
    out = str(tmp_path / "enc.db.age")
    rc, o = admin(r, "backup", out, "--age-recipient", "fake-recipient")
    assert rc == 0 and "encrypted" in o.out
    raw = open(out, "rb").read()
    assert raw.startswith(backup_mod.AGE_HEADER) and b"SQLite format 3" not in raw
    assert not [f for f in os.listdir(tmp_path) if ".plain." in f]                    # plaintext removed
    ident = tmp_path / "id"
    ident.write_text("wrong-identity")
    r.stop()
    rc, o = admin(r, "restore", out)
    assert rc == 5 and "--age-identity" in o.err
    rc, o = admin(r, "restore", out, "--age-identity", str(ident))
    assert rc == 5 and "age failed" in o.err
    ident.write_text("fake-recipient")
    rc, o = admin(r, "restore", out, "--age-identity", str(ident))
    assert rc == 0, o.err
    assert not [f for f in os.listdir(os.path.dirname(r.db)) if ".restore." in f]       # decrypted copy removed


@pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age is not installed here")
def test_real_age_round_trip(r, tmp_path):                                              # pragma: no cover - host dependent
    import subprocess
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = [l.split(": ")[1] for l in key.read_text().splitlines() if l.startswith("# public key:")][0]
    out = str(tmp_path / "real.db.age")
    assert admin(r, "backup", out, "--age-recipient", recipient)[0] == 0
    r.stop()
    assert admin(r, "restore", out, "--age-identity", str(key))[0] == 0


def test_total_host_loss_uses_the_off_host_journal_copy(r):
    tok = r.mint("jose/claude")
    out = take(r)
    admin(r, "revoke-token", tok.split("_")[1])
    out2 = take(r, "b2.db")                                    # the latest journal copy, now "off-host"
    shutil.copyfile(out2 + ".journal", str(r.tmp / "offhost.journal"))
    os.remove(r.app.state.journal_path)                        # the host's journal is gone
    rc, o = restore(r, out, "--journal-from", str(r.tmp / "offhost.journal"))
    assert rc == 0, o.err
    assert r.client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_restore_refuses_bad_files(r):
    junk = r.tmp / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    r.stop()
    rc, o = admin(r, "restore", str(junk))
    assert rc == 5
    plain = r.tmp / "plain.db"
    sqlite3.connect(plain).execute("CREATE TABLE meta(key, value)").connection.commit()
    rc, o = admin(r, "restore", str(plain))
    assert rc == 5 and "not a Glassboard backup" in o.err


def test_restore_itself_reapplies_the_journal_before_any_server_starts(r):
    """The restore command must leave a correct database on its own (an operator may inspect it, or
    the server may start much later); the server's startup replay is a second line, not the first."""
    tok = r.mint("jose/claude")
    out = take(r)
    admin(r, "revoke-token", tok.split("_")[1])
    r.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=r.rec())
    r.stop()
    r.raw.close()
    rc, o = admin(r, "restore", out)
    assert rc == 0, o.err
    raw = sqlite3.connect(r.db)                            # no app has started since the restore
    assert raw.execute("SELECT revoked_at FROM credential WHERE id=?", (tok.split("_")[1],)).fetchone()[0]
    assert raw.execute("SELECT left_at FROM member m JOIN principal p ON p.id = m.principal_id WHERE p.handle='ana'").fetchone()[0]
    wm = int(raw.execute("SELECT value FROM meta WHERE key='journal_applied_n'").fetchone()[0])
    assert wm == 2                                          # the watermark caught up with the journal
    assert raw.execute("SELECT count(*) FROM event WHERE type IN ('member.removed') AND actor_id=?",
                       (ids.BOARD_PRINCIPAL_ID,)).fetchone()[0] == 0   # covered by system.restored, no per-entry event
    raw.close()


def test_restore_refuses_missing_inputs_before_changing_anything(r):
    """Found by drill D7: with no access reduction yet there is no journal, so no `<backup>.journal`
    copy; `--journal-from` that file used to crash with a traceback. Now: a clean refusal (exit 5)
    and the database is untouched. Same for a missing backup file."""
    out = take(r)
    assert not os.path.exists(out + ".journal")
    api(r, "POST", "/messages", "jose", {"kind": "note", "body": "after the backup"})
    head = r.head()
    r.stop()
    rc, o = admin(r, "restore", out, "--journal-from", out + ".journal")
    assert rc == 5 and "does not exist" in o.err and "Traceback" not in o.err
    rc, o = admin(r, "restore", str(r.tmp / "nope.db"))
    assert rc == 5 and "does not exist" in o.err and "Traceback" not in o.err
    assert r.head() == head                                     # nothing was restored
    rc, o = admin(r, "restore", out)                            # without the option it works
    assert rc == 0, o.err
