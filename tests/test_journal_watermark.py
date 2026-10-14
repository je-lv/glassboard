"""Startup replay applies only journal entries newer than the database's watermark, so a
restart never undoes a later, legitimate grant; a restored backup still gets every newer reduction."""
import os

import pytest

from glassboard import admin, journal
from glassboard.app import create_app
from glassboard.dashboard import COOKIE, LOGIN_COOKIE  # noqa: F401
from fastapi.testclient import TestClient


def test_at_e17_1_readded_member_survives_a_restart(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())
    gb.add("s1", "ana")
    head = gb.head()
    gb.restart()
    gb.restart()
    assert gb.req("GET", "/api/v1/sessions/s1/members", "ana", key=None).status_code == 200
    assert gb.head() == head                                         # no replay event either


def test_at_e17_2_promotion_after_a_reduction_survives_a_restart(gb):
    gb.session("s1")
    gb.add("s1", "ana", role="coordinator")
    gb.add("s1", "ana", role="member")                                # journaled reduction
    gb.add("s1", "ana", role="coordinator")                           # later grant
    gb.restart()
    assert gb.raw.execute("SELECT role FROM member WHERE principal_id=?", (gb.pid("ana"),)).fetchone()[0] == "coordinator"


def test_at_e17_3_new_web_sessions_survive_a_restart_after_revoke_all(gb):
    from test_dashboard import login
    gb.session("s1")
    login(gb)
    gb.admin("revoke-web-sessions", "jose")
    c = login(gb)                                                     # logs in again afterwards
    gb.restart()
    c2 = TestClient(gb.app, base_url="https://testserver", follow_redirects=False)
    c2.cookies = c.cookies
    assert c2.get("/s/s1").status_code == 200


def test_at_e17_4_restored_backup_still_gets_newer_reductions(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    backup = str(gb.tmp / "backup.db")
    import sqlite3
    dst = sqlite3.connect(backup)
    gb.raw.backup(dst)                     # SQLite online backup: includes pages still in the WAL
    dst.close()
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())   # after the backup
    gb.add("s1", "ana")                                               # re-added after the backup too
    gb.app.state.db.close()
    gb.raw.close()
    os.replace(backup, gb.db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(gb.db + suffix):
            os.remove(gb.db + suffix)
    import sqlite3
    gb.raw = sqlite3.connect(gb.db, isolation_level=None)
    gb.raw.row_factory = sqlite3.Row
    gb.restart()
    # the removal is re-applied, the post-backup re-add is lost (fail closed)
    assert gb.req("GET", "/api/v1/sessions/s1/members", "ana", key=None).status_code == 404


def test_at_e17_5_a_database_ahead_of_its_journal_refuses_to_start(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())
    gb.app.state.db.close()
    os.remove(gb.app.state.journal_path)                              # journal lost
    with pytest.raises(journal.JournalError, match="truncated or replaced"):
        create_app(gb.db, allowed_hosts=["testserver"])


def test_watermark_moves_with_every_journaled_change(gb):
    gb.session("s1")
    gb.add("s1", "ana")
    gb.req("DELETE", "/api/v1/sessions/s1/members/ana", "jose", rec=gb.rec())
    gb.admin("deactivate", "ana/codex")
    assert gb.raw.execute("SELECT value FROM meta WHERE key='journal_applied_n'").fetchone()[0] == "2"
