"""Verify and seal the private evidence of one multi-client project rehearsal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from .run_evidence import _hash, _jsonl_status, _write_json

REQUIRED = (
    "brief.txt", "roster.json", "board-events.jsonl", "board-snapshot.json",
    "role-revisions.json", "artifact-manifest.json", "interventions.jsonl", "supervisor.jsonl",
    "checks.json", "project-baseline.bundle", "project-first-submission.tar",
)
AGENT_RE = re.compile(r"^[a-z0-9_-]+$")
ROUND_RE = re.compile(r"^([a-z0-9_-]+)-round([0-9]{2,})$")


class EvidenceGap(ValueError):
    """A required artifact is missing, malformed, or altered."""


def _under(root: Path, relative: str) -> Path:
    name = Path(relative)
    if not relative or name.is_absolute() or any(p in (".", "..") for p in name.parts):
        raise EvidenceGap("artifact path is not relative to the evidence directory")
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise EvidenceGap(f"artifact escapes the evidence directory: {relative}")
    return path


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise EvidenceGap(f"invalid JSON artifact: {path.name}") from exc


def _check_round(root: Path, agent: str, number: int, directory: Path) -> dict:
    record = _json(directory / "record.json")
    if not isinstance(record, dict):
        raise EvidenceGap(f"invalid round record: {directory.name}")
    if (record.get("agent") != agent or record.get("round") != number
            or record.get("status") != "complete" or record.get("exit_code") != 0
            or record.get("invalid_json_lines") != 0
            or not all(record.get(key) for key in ("started_at", "ended_at", "argv", "cwd"))):
        raise EvidenceGap(f"incomplete client round: {directory.name}")
    for name, key in (("prompt.txt", "prompt_sha256"), ("stdout.jsonl", "stdout_sha256"),
                      ("stderr.bin", "stderr_sha256")):
        path = directory / name
        if not path.is_file() or path.is_symlink() or _hash(path) != record.get(key):
            raise EvidenceGap(f"missing or changed {name} in {directory.name}")
    valid, invalid = _jsonl_status(directory / "stdout.jsonl")
    if not valid or invalid or valid != record.get("json_events"):
        raise EvidenceGap(f"missing or malformed JSON events in {directory.name}")
    native_files = record.get("native_files")
    if not isinstance(native_files, list):
        raise EvidenceGap(f"native history disposition missing in {directory.name}")
    reason = record.get("native_unavailable_reason")
    if not native_files and (not isinstance(reason, str) or not reason.strip()):
        raise EvidenceGap(f"native history absence unexplained in {directory.name}")
    for item in native_files:
        if not isinstance(item, dict):
            raise EvidenceGap(f"invalid native history entry in {directory.name}")
        if item.get("status") != "copied":
            raise EvidenceGap(f"native history gap in {directory.name}")
        name = item.get("copy", "")
        if not re.fullmatch(r"native-[0-9]{2,}(?:\.[A-Za-z0-9]+)?", name):
            raise EvidenceGap(f"invalid native history path in {directory.name}")
        path = directory / name
        if not path.is_file() or path.is_symlink() or _hash(path) != item.get("sha256"):
            raise EvidenceGap(f"missing or changed native history in {directory.name}")
    return {"agent": agent, "round": number, "events": valid, "exit_code": record.get("exit_code")}


def seal(root: Path) -> dict:
    """Refuse to seal when a selected client or required evidence has a gap."""
    if root.is_symlink() or not root.is_dir():
        raise EvidenceGap("evidence directory is missing or a symlink")
    root = root.resolve()
    if root.stat().st_mode & 0o077:
        raise EvidenceGap("evidence directory must be mode 0700")
    for name in REQUIRED:
        path = _under(root, name)
        if not path.is_file() or path.is_symlink():
            raise EvidenceGap(f"required artifact missing: {name}")
        if name not in ("interventions.jsonl",) and path.stat().st_size == 0:
            raise EvidenceGap(f"required artifact is empty: {name}")
    roster = _json(root / "roster.json")
    agents = roster.get("agents") if isinstance(roster, dict) else None
    if not isinstance(agents, list) or not agents or any(not isinstance(a, str) or not AGENT_RE.fullmatch(a) for a in agents):
        raise EvidenceGap("roster.json needs a nonempty agents list")
    if len(set(agents)) != len(agents):
        raise EvidenceGap("roster.json repeats an agent")
    board_events, bad = _jsonl_status(root / "board-events.jsonl")
    if not board_events or bad:
        raise EvidenceGap("board-events.jsonl has no valid event stream")
    board_rows = [json.loads(line) for line in (root / "board-events.jsonl").read_text().splitlines()]
    sequence = [row.get("seq") for row in board_rows]
    if sequence != list(range(1, len(sequence) + 1)):
        raise EvidenceGap("board-events.jsonl has missing or out-of-order events")
    trace_path = root / "board-trace.jsonl"
    expected_trace = [row for row in board_rows if row.get("type") == "message.preflight"]
    if expected_trace or trace_path.exists():
        if not trace_path.is_file() or trace_path.is_symlink():
            raise EvidenceGap("forensic trace is missing or is not an ordinary file")
        _, bad = _jsonl_status(trace_path)
        if bad or [json.loads(line) for line in trace_path.read_text().splitlines()] != expected_trace:
            raise EvidenceGap("forensic trace differs from the complete board record")
    board = _json(root / "board-snapshot.json")
    roles = _json(root / "role-revisions.json")
    if not isinstance(board, dict) or not {"board", "members", "flow"} <= board.keys() or not isinstance(roles, dict) \
            or not {"catalog", "assignments", "revisions"} <= roles.keys():
        raise EvidenceGap("board or role snapshot is not a JSON object")
    artifact_manifest = _json(root / "artifact-manifest.json")
    artifact_items = artifact_manifest.get("artifacts") if isinstance(artifact_manifest, dict) else None
    if not isinstance(artifact_manifest, dict) or artifact_manifest.get("schema") != 1 \
            or not isinstance(artifact_items, list):
        raise EvidenceGap("artifact manifest is malformed")
    artifact_ids = set()
    for item in artifact_items:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(item.get("id", ""))):
            raise EvidenceGap("artifact manifest is malformed")
        artifact_id = item["id"]
        if artifact_id in artifact_ids or item.get("path") != f"artifacts/{artifact_id}.bin":
            raise EvidenceGap("artifact manifest has a duplicate or invalid path")
        artifact_ids.add(artifact_id)
        path = _under(root, item["path"])
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != item.get("size")
                or _hash(path) != item.get("sha256")):
            raise EvidenceGap(f"artifact is missing or changed: {artifact_id}")
    event_artifacts = {}
    for row in board_rows:
        if row.get("type") != "artifact.added":
            continue
        data = row.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("artifact"), str) \
                or data["artifact"] in event_artifacts:
            raise EvidenceGap("artifact event is malformed or duplicated")
        event_artifacts[data["artifact"]] = data
    if set(event_artifacts) != artifact_ids:
        raise EvidenceGap("artifact manifest differs from artifact.added events")
    for item in artifact_items:
        event = event_artifacts[item["id"]]
        if event.get("size") != item.get("size") or event.get("sha256") != item.get("sha256"):
            raise EvidenceGap(f"artifact manifest differs from event: {item['id']}")
    _, bad = _jsonl_status(root / "interventions.jsonl")
    if bad:
        raise EvidenceGap("interventions.jsonl is malformed")
    supervisor_events, bad = _jsonl_status(root / "supervisor.jsonl")
    if not supervisor_events or bad:
        raise EvidenceGap("supervisor.jsonl has no valid event stream")
    checks = _json(root / "checks.json")
    if not isinstance(checks, dict) or "results" not in checks:
        raise EvidenceGap("checks.json needs a results field")
    found: dict[str, list[int]] = {a: [] for a in agents}
    rounds = []
    for child in root.iterdir():
        match = ROUND_RE.fullmatch(child.name)
        if match is None:
            continue
        agent, number = match.group(1), int(match.group(2))
        if agent not in found or not child.is_dir() or child.is_symlink():
            raise EvidenceGap(f"unexpected round directory: {child.name}")
        found[agent].append(number)
        rounds.append(_check_round(root, agent, number, child))
    for agent, numbers in found.items():
        if sorted(numbers) != list(range(1, len(numbers) + 1)) or not numbers:
            raise EvidenceGap(f"missing or nonconsecutive rounds for {agent}")
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if path.parent == root and name in ("runtime", "tokens", "client-home"):
                raise EvidenceGap("client credentials must be kept outside the evidence archive")
            if path.is_symlink():
                raise EvidenceGap(f"symlink in evidence: {path.relative_to(root)}")
            if path.stat().st_mode & 0o077:
                raise EvidenceGap(f"evidence artifact is readable by others: {path.relative_to(root)}")
        for name in names:
            path = Path(current) / name
            relative = str(path.relative_to(root))
            if relative in ("manifest.json", "manifest.json.tmp"):
                continue
            files.append({"path": relative, "bytes": path.stat().st_size, "sha256": _hash(path)})
    manifest = {"schema": 2, "agents": agents, "rounds": sorted(rounds, key=lambda r: (r["agent"], r["round"])),
                "board_events": board_events, "files": sorted(files, key=lambda f: f["path"]),
                "provider_private_reasoning_captured": False}
    _write_json(root / "manifest.json", manifest)
    verify(root)
    return manifest


def verify(root: Path) -> dict:
    """Check every sealed artifact and reject later additions or changes."""
    if root.is_symlink() or not root.is_dir():
        raise EvidenceGap("evidence directory is missing or a symlink")
    root = root.resolve()
    manifest = _json(root / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema") not in (1, 2) or not isinstance(manifest.get("files"), list):
        raise EvidenceGap("invalid evidence manifest")
    listed = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in listed:
            raise EvidenceGap("invalid or duplicate manifest path")
        relative = item["path"]
        path = _under(root, relative)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item.get("bytes") or _hash(path) != item.get("sha256"):
            raise EvidenceGap(f"sealed artifact changed: {relative}")
        listed[relative] = True
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    actual.discard("manifest.json")
    if actual != set(listed):
        raise EvidenceGap("sealed artifact list differs from evidence directory")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--verify", action="store_true", help="verify an existing seal without changing evidence")
    args = parser.parse_args(argv)
    try:
        manifest = verify(args.evidence) if args.verify else seal(args.evidence)
    except EvidenceGap as exc:
        print(f"Evidence incomplete: {exc}", file=sys.stderr)
        return 1
    print(f"{'Verified' if args.verify else 'Sealed'} {len(manifest['agents'])} agents, {len(manifest['rounds'])} rounds, "
          f"{manifest['board_events']} board events, {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
