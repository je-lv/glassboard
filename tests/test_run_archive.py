"""Private run archives must identify gaps before post-run analysis."""
import json
from pathlib import Path
import sys

import pytest

from glassboard.run_archive import EvidenceGap, seal, verify
from glassboard.run_evidence import capture


def _file(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    _file(root, "brief.txt", b"Build the project.\n")
    _file(root, "roster.json", b'{"agents":["codex","claude","grok"]}\n')
    _file(root, "board-events.jsonl", b'{"seq":1,"kind":"session.created"}\n')
    _file(root, "board-snapshot.json", b'{"board":{},"members":[],"flow":{}}\n')
    _file(root, "role-revisions.json", b'{"catalog":[],"assignments":[],"revisions":[]}\n')
    _file(root, "artifact-manifest.json", b'{"schema":1,"session":"test","artifacts":[]}\n')
    _file(root, "interventions.jsonl", b"")
    _file(root, "supervisor.jsonl", b'{"kind":"started"}\n')
    _file(root, "checks.json", b'{"results":[]}\n')
    _file(root, "project-baseline.bundle", b"baseline")
    _file(root, "project-first-submission.tar", b"submission")
    prompt = _file(tmp_path, "prompt.txt", b"Exact prompt\n")
    for agent in ("codex", "claude", "grok"):
        capture(root, agent, 1, prompt, tmp_path,
                [sys.executable, "-c", "print('{\"type\":\"result\"}')"],
                native_unavailable_reason="synthetic client has no native history")
    return root


def test_archive_seals_full_observable_json_and_all_agents(tmp_path: Path):
    root = _archive(tmp_path)
    manifest = seal(root)
    assert manifest["schema"] == 2
    assert manifest["agents"] == ["codex", "claude", "grok"]
    assert manifest["board_events"] == 1
    assert [item["events"] for item in manifest["rounds"]] == [1, 1, 1]
    assert manifest["provider_private_reasoning_captured"] is False
    assert (root / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert any(item["path"] == "codex-round01/stdout.jsonl" for item in manifest["files"])
    assert verify(root) == manifest


def test_archive_detects_post_seal_log_append(tmp_path: Path):
    root = _archive(tmp_path)
    _file(root, "supervisor-console.log", b"run stopped\n")
    seal(root)
    with (root / "supervisor-console.log").open("ab") as log:
        log.write(b"Evidence sealed\n")
    with pytest.raises(EvidenceGap, match="supervisor-console.log"):
        verify(root)


@pytest.mark.parametrize("damage", ["missing_round", "changed_json", "bad_board_sequence",
                                    "public_file", "credential_dir", "missing_supervisor"])
def test_archive_refuses_gaps(tmp_path: Path, damage: str):
    root = _archive(tmp_path)
    if damage == "missing_round":
        (root / "grok-round01" / "record.json").unlink()
    elif damage == "changed_json":
        _file(root, "codex-round01/stdout.jsonl", b"not json\n")
    elif damage == "bad_board_sequence":
        _file(root, "board-events.jsonl", b'{"seq":2}\n')
    elif damage == "public_file":
        (root / "brief.txt").chmod(0o644)
    elif damage == "credential_dir":
        (root / "tokens").mkdir(mode=0o700)
    else:
        (root / "supervisor.jsonl").unlink()
    with pytest.raises(EvidenceGap):
        seal(root)


def test_archive_refuses_missing_sequence_field(tmp_path: Path):
    root = _archive(tmp_path)
    _file(root, "board-events.jsonl", b'{"kind":"message.posted"}\n')
    with pytest.raises(EvidenceGap, match="out-of-order"):
        seal(root)


def test_archive_requires_exact_forensic_trace_subset(tmp_path: Path):
    root = _archive(tmp_path)
    first = {"seq": 1, "type": "session.created"}
    draft = {"seq": 2, "type": "message.preflight", "body": '{"kind":"note","body":"unsent"}'}
    _file(root, "board-events.jsonl", (json.dumps(first) + "\n" + json.dumps(draft) + "\n").encode())
    with pytest.raises(EvidenceGap, match="forensic trace is missing"):
        seal(root)
    trace = _file(root, "board-trace.jsonl", (json.dumps(draft) + "\n").encode())
    assert seal(root)["board_events"] == 2
    trace.write_text(json.dumps(first) + "\n")
    with pytest.raises(EvidenceGap):
        verify(root)
    with pytest.raises(EvidenceGap, match="forensic trace differs"):
        seal(root)


def test_archive_requires_exact_artifact_bytes(tmp_path: Path):
    root = _archive(tmp_path)
    artifact_id = "01JTESTARTIFACT00000000000"
    artifacts = root / "artifacts"
    artifacts.mkdir(mode=0o700)
    payload = _file(artifacts, artifact_id + ".bin", b"review evidence\n")
    import hashlib
    manifest = {"schema": 1, "session": "test", "artifacts": [{"id": artifact_id,
                "path": f"artifacts/{artifact_id}.bin", "size": payload.stat().st_size,
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}]}
    _file(root, "artifact-manifest.json", (json.dumps(manifest) + "\n").encode())
    event = {"seq": 2, "type": "artifact.added", "data": {"artifact": artifact_id,
             "size": payload.stat().st_size, "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}}
    _file(root, "board-events.jsonl", b'{"seq":1,"type":"session.created"}\n' +
          (json.dumps(event) + "\n").encode())
    seal(root)
    payload.write_bytes(b"changed\n")
    with pytest.raises(EvidenceGap, match="artifact"):
        seal(root)
