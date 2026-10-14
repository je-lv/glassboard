import hashlib
import sqlite3

import pytest

from glassboard import ids
from test_dashboard import login


def setup_board(gb):
    gb.session()
    gb.add("s1", "ana")
    gb.add("s1", "jose/claude")
    gb.add("s1", "ana/codex", role="observer")


def upload(gb, who, content=b"report\n", **query):
    from urllib.parse import urlencode
    q = {"name": "report.txt", "media_type": "text/plain", **query}
    return gb.client.post("/api/v1/sessions/s1/artifacts?" + urlencode(q), content=content,
                          headers=gb.headers(who, rec=gb.rec(), key=ids.new_ulid(),
                                             extra={"Content-Type": "application/octet-stream"}))


def test_artifact_is_immutable_hashed_task_linked_and_downloaded_as_attachment(gb):
    setup_board(gb)
    task = gb.req("POST", "/api/v1/sessions/s1/tasks", "jose",
                  json={"title": "Audit", "scope": "x", "deliverable": "report"}, rec=gb.rec()).json()["task"]
    content = b"plain audit evidence\n"
    r = upload(gb, "jose/claude", content, task=f"T{task}", description="Audit output")
    assert r.status_code == 201, r.text
    result = r.json()
    assert result["sha256"] == hashlib.sha256(content).hexdigest() and result["task"] == task
    assert result["purpose"] == "evidence"
    event = gb.events()[-1]
    assert event["type"] == "artifact.added" and event["task_num"] == task
    assert event["data"]["artifact"] == result["artifact"] and event["data"]["sha256"] == result["sha256"]
    listed = gb.req("GET", "/api/v1/sessions/s1/artifacts", "ana", key=None).json()["artifacts"]
    assert listed[0]["id"] == result["artifact"] and "content" not in listed[0]
    got = gb.req("GET", f"/api/v1/sessions/s1/artifacts/{result['artifact']}/content", "ana", key=None)
    assert got.content == content and got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment;")
    with pytest.raises(sqlite3.IntegrityError):
        gb.raw.execute("UPDATE artifact SET name='changed' WHERE id=?", (result["artifact"],))


def test_artifact_authorization_limits_and_secret_guard(gb):
    setup_board(gb)
    assert upload(gb, "ana/codex").status_code == 403
    assert upload(gb, "jose/claude", b"gbt_abcdefghij_" + b"A" * 43).json()["error"]["code"] == "secret_in_content"
    too_big = b"x" * (16 * 1024 * 1024 + 1)
    assert upload(gb, "jose/claude", too_big).status_code == 413
    assert gb.raw.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 0
    assert upload(gb, "jose/claude", purpose="release").status_code == 422


def test_deliverable_is_classified_in_api_browser_and_cli(gb, live_server, tmp_path):
    setup_board(gb)
    result = upload(gb, "jose/claude", content=b"final report\n", purpose="deliverable").json()
    assert result["purpose"] == "deliverable"
    assert gb.events()[-1]["data"]["purpose"] == "deliverable"
    assert gb.req("GET", "/api/v1/sessions/s1/artifacts", "ana", key=None).json()["artifacts"][0]["purpose"] == "deliverable"
    page = login(gb).get("/s/s1/artifacts")
    assert "Final deliverables" in page.text and "report.txt" in page.text

    assert live_server.cli("jose/claude", "use", "s1").returncode == 0
    source = tmp_path / "package.zip"
    source.write_bytes(b"release bytes")
    sent = live_server.cli("jose/claude", "artifact", "upload", str(source), "--deliverable")
    assert sent.returncode == 0, sent.stderr
    listing = live_server.cli("ana", "artifact", "list", "-s", "s1")
    assert listing.returncode == 0 and "deliverable" in listing.stdout


def test_browser_artifact_page_upload_and_download(gb):
    setup_board(gb)
    client = login(gb)
    page = client.get("/s/s1/artifacts")
    assert page.status_code == 200 and "Upload immutable copy" in page.text
    import re
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    recovery = re.search(r'data-recovery="([^"]+)"', page.text).group(1)
    op = re.search(r'data-op="([^"]+)"', page.text).group(1)
    r = client.post("/s/s1/artifacts/upload?name=screen.png&media_type=image%2Fpng&task=",
                    content=b"PNG evidence", headers={"X-Glassboard-CSRF": csrf,
                    "X-Glassboard-Recovery": recovery, "Idempotency-Key": op,
                    "Content-Type": "application/octet-stream", "Origin": "https://testserver"})
    assert r.status_code == 201, r.text
    artifact = r.json()["artifact"]
    page = client.get("/s/s1/artifacts")
    assert "screen.png" in page.text and r.json()["sha256"] in page.text
    got = client.get(f"/s/s1/artifacts/{artifact}/download")
    assert got.content == b"PNG evidence" and got.headers["content-disposition"].startswith("attachment;")


def test_cli_artifact_round_trip(gb, live_server, tmp_path):
    setup_board(gb)
    assert live_server.cli("jose/claude", "use", "s1").returncode == 0
    source = tmp_path / "evidence.txt"
    source.write_text("verified evidence\n")
    sent = live_server.cli("jose/claude", "artifact", "upload", str(source), "--task", "T1")
    assert sent.returncode == 5 and "not_found" in sent.stderr       # task link is validated
    sent = live_server.cli("jose/claude", "artifact", "upload", str(source))
    assert sent.returncode == 0, sent.stderr
    artifact = sent.stdout.split()[1]
    listing = live_server.cli("ana", "artifact", "list", "-s", "s1")
    assert listing.returncode == 0 and artifact in listing.stdout and "evidence.txt" in listing.stdout
    out = tmp_path / "downloaded.txt"
    got = live_server.cli("ana", "artifact", "get", artifact, "-s", "s1", "--out", str(out))
    assert got.returncode == 0, got.stderr
    assert out.read_bytes() == source.read_bytes()
