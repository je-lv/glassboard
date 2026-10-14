"""Board evidence exports preserve the complete event and role revision history."""
import json
from pathlib import Path
import sys

import pytest

from glassboard.run_export import ExportError, export


def test_export_board_events_and_all_role_revisions(live_server, tmp_path: Path):
    gb = live_server.gb
    gb.session("s1")
    gb.add("s1", "jose/claude")
    cli = [sys.executable, "-m", "glassboard.cli"]
    result = export(tmp_path / "private", "s1", cli, live_server.env("jose"))
    assert result["events"] == gb.head()
    root = tmp_path / "private"
    events = [json.loads(line) for line in (root / "board-events.jsonl").read_text().splitlines()]
    assert [event["seq"] for event in events] == list(range(1, gb.head() + 1))
    roles = json.loads((root / "role-revisions.json").read_text())
    assert roles["revisions"]["architect"][0]["version"] == 1
    assert len(roles["assignments"]) == 2
    artifact_manifest = json.loads((root / "artifact-manifest.json").read_text())
    assert artifact_manifest == {"schema": 1, "session": "s1", "artifacts": []}
    assert all((root / name).stat().st_mode & 0o777 == 0o600 for name in
               ("board-events.jsonl", "board-trace.jsonl", "board-snapshot.json", "role-revisions.json",
                "artifact-manifest.json"))


def test_export_preserves_immutable_artifact_bytes_and_hash(live_server, tmp_path: Path):
    gb = live_server.gb
    gb.session("s1")
    content = b"exact report bytes\n"
    uploaded = gb.client.post("/api/v1/sessions/s1/artifacts?name=report.txt&media_type=text%2Fplain",
                              content=content, headers=gb.headers("jose", rec=gb.rec())).json()
    root = tmp_path / "private"
    export(root, "s1", [sys.executable, "-m", "glassboard.cli"], live_server.env("jose"))
    manifest = json.loads((root / "artifact-manifest.json").read_text())
    item = manifest["artifacts"][0]
    assert item["id"] == uploaded["artifact"] and item["sha256"] == uploaded["sha256"]
    artifact = root / item["path"]
    assert artifact.read_bytes() == content and artifact.stat().st_mode & 0o777 == 0o600


def test_export_carries_preflight_in_complete_and_trace_records(live_server, tmp_path: Path):
    gb = live_server.gb
    gb.session("s1")
    checked = gb.req("POST", "/api/v1/sessions/s1/messages/preflight", "jose",
                     json={"kind": "note", "body": "Draft for later analysis."}, rec=gb.rec())
    assert checked.status_code == 200
    root = tmp_path / "private"
    cli = [sys.executable, "-m", "glassboard.cli"]
    export(root, "s1", cli, live_server.env("jose"))
    all_events = [json.loads(line) for line in (root / "board-events.jsonl").read_text().splitlines()]
    trace = [json.loads(line) for line in (root / "board-trace.jsonl").read_text().splitlines()]
    assert trace == [e for e in all_events if e["type"] == "message.preflight"]
    assert trace[0]["body"] == '{"kind":"note","body":"Draft for later analysis."}'


def test_export_refuses_missing_session(live_server, tmp_path: Path):
    cli = [sys.executable, "-m", "glassboard.cli"]
    with pytest.raises(ExportError):
        export(tmp_path / "private", "missing", cli, live_server.env("jose"))
