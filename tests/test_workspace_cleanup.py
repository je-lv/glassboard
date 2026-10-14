import hashlib
import sqlite3
import zipfile

import pytest

from glassboard import db, ids, journal, workspace_archive
from test_dashboard import csrf_of, login


def archived_workspace(gb):
    gb.session()
    gb.add("s1", "jose/claude")
    content = b"final report\n"
    response = gb.client.post(
        "/api/v1/sessions/s1/artifacts?name=report.txt&media_type=text%2Fplain&purpose=deliverable",
        content=content,
        headers=gb.headers("jose/claude", rec=gb.rec(), key=ids.new_ulid(),
                           extra={"Content-Type": "application/octet-stream"}),
    )
    assert response.status_code == 201, response.text
    changed = gb.req("POST", "/api/v1/sessions/s1/state", "jose",
                     json={"state": "archived", "reason": "run complete"}, rec=gb.rec())
    assert changed.status_code == 200, changed.text
    return content


def test_verified_archive_contains_public_record_and_purge_is_journaled(gb):
    content = archived_workspace(gb)
    result = workspace_archive.create_archive(gb.app.state.db, "s1", gb.pid("jose"))
    archive = workspace_archive.archive_record(gb.app.state.db, result["id"])
    verified = workspace_archive.verify_archive(workspace_archive.archive_path(gb.app.state.db, archive), archive)
    assert verified["manifest"]["counts"]["artifacts"] == 1
    with zipfile.ZipFile(result["path"]) as package:
        assert package.read("artifacts.json").find(b'"purpose": "deliverable"') >= 0
        assert any(hashlib.sha256(package.read(item.filename)).hexdigest() == hashlib.sha256(content).hexdigest()
                   for item in package.infolist() if item.filename.startswith("artifacts/"))

    purged = workspace_archive.purge_workspace(
        gb.app.state.db, gb.app.state.journal_path, "s1", result["id"], gb.pid("jose"))
    assert purged["archive_id"] == result["id"]
    assert gb.raw.execute("SELECT 1 FROM session WHERE handle='s1'").fetchone() is None
    assert gb.raw.execute("SELECT session_handle FROM workspace_purge").fetchone()[0] == "s1"
    assert gb.req("POST", "/api/v1/sessions", "jose", json={"handle": "s1", "title": "reuse"}).status_code == 409


def test_tampered_archive_refuses_purge(gb):
    archived_workspace(gb)
    result = workspace_archive.create_archive(gb.app.state.db, "s1", gb.pid("jose"))
    path = workspace_archive.archive_path(gb.app.state.db, workspace_archive.archive_record(gb.app.state.db, result["id"]))
    with path.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(workspace_archive.WorkspaceArchiveError):
        workspace_archive.purge_workspace(
            gb.app.state.db, gb.app.state.journal_path, "s1", result["id"], gb.pid("jose"))
    assert gb.raw.execute("SELECT state FROM session WHERE handle='s1'").fetchone()[0] == "archived"


def test_journal_replay_removes_workspace_from_older_database_backup(gb, tmp_path):
    archived_workspace(gb)
    old_backup = tmp_path / "older.db"
    copied = sqlite3.connect(old_backup)
    gb.raw.backup(copied)
    copied.close()

    assert gb.admin("add-human", "late-admin", "--admin")[0] == 0
    result = workspace_archive.create_archive(gb.app.state.db, "s1", gb.pid("late-admin"))
    workspace_archive.purge_workspace(
        gb.app.state.db, gb.app.state.journal_path, "s1", result["id"], gb.pid("late-admin"))
    restored = db.Database(str(old_backup))
    try:
        journal.replay(restored, gb.app.state.journal_path, emit_events=False)
        with restored.read() as conn:
            assert conn.execute("SELECT 1 FROM session WHERE handle='s1'").fetchone() is None
            assert conn.execute("SELECT session_handle FROM workspace_purge").fetchone()[0] == "s1"
    finally:
        restored.close()


def test_admin_operations_require_admin_and_exact_confirmation(gb):
    archived_workspace(gb)
    ordinary = login(gb)
    assert ordinary.get("/admin?tab=operations").status_code == 404
    gb.raw.execute("UPDATE principal SET is_admin=1 WHERE handle='jose'")
    admin = login(gb)
    page = admin.get("/admin?tab=operations")
    assert page.status_code == 200 and "Workspace retention" in page.text
    csrf = csrf_of(page.text)
    refused = admin.post("/admin/workspaces/archive", data={"csrf": csrf, "handle": "s1", "confirm": "wrong"})
    assert refused.status_code == 303 and "confirmation_required" in refused.headers["location"]
    made = admin.post("/admin/workspaces/archive", data={"csrf": csrf, "handle": "s1", "confirm": "s1"})
    assert made.status_code == 303
    archive_id = gb.raw.execute("SELECT id FROM workspace_archive").fetchone()[0]
    download = admin.get(f"/admin/workspace-archives/{archive_id}")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    purged = admin.post("/admin/workspaces/purge", data={"csrf": csrf, "handle": "s1", "confirm": "s1",
                                                                "archive_id": archive_id, "irreversible": "1"})
    assert purged.status_code == 303
    assert gb.raw.execute("SELECT 1 FROM session WHERE handle='s1'").fetchone() is None
