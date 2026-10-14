"""Export a complete, private Glassboard session and role history for run evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


class ExportError(ValueError):
    """The board did not return a complete export."""


def _call(cli: list[str], args: list[str], env: dict[str, str]) -> dict:
    proc = subprocess.run([*cli, *args], env=env, capture_output=True, timeout=60, check=False)
    if proc.returncode:
        raise ExportError(f"Glassboard command failed: {args[0]}")
    try:
        result = json.loads(proc.stdout)
    except (UnicodeError, ValueError) as exc:
        raise ExportError(f"Glassboard returned invalid JSON: {args[0]}") from exc
    if not isinstance(result, dict):
        raise ExportError(f"Glassboard returned a non-object: {args[0]}")
    return result


def _download(cli: list[str], session: str, artifact_id: str, destination: Path,
              env: dict[str, str]) -> None:
    proc = subprocess.run([*cli, "artifact", "get", artifact_id, "-s", session,
                           "--out", str(destination)], env=env, capture_output=True,
                          timeout=60, check=False)
    if proc.returncode:
        raise ExportError(f"Glassboard command failed: artifact get {artifact_id}")


def _save(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def export(evidence: Path, session: str, cli: list[str], env: dict[str, str] | None = None) -> dict:
    """Fetch all events, current board state, and every versioned role prompt."""
    if not session or not cli or evidence.is_symlink():
        raise ExportError("session, CLI, and ordinary evidence directory are required")
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    if evidence.stat().st_mode & 0o077:
        raise ExportError("evidence directory must be private (mode 0700)")
    environment = dict(os.environ if env is None else env)
    events = []
    after = 0
    while True:
        page = _call(cli, ["history", "-s", session, "--after", str(after), "--limit", "100", "--json"], environment)
        rows = page.get("events")
        if not isinstance(rows, list) or not rows:
            if not events or page.get("more"):
                raise ExportError("board history is empty or failed to advance")
            break
        for row in rows:
            if not isinstance(row, dict) or row.get("seq") != len(events) + 1:
                raise ExportError("board history has missing or out-of-order events")
            events.append(row)
        if not page.get("more"):
            break
        next_after = page.get("next_after")
        if not isinstance(next_after, int) or next_after <= after:
            raise ExportError("board history pagination failed to advance")
        after = next_after
    snapshot = {
        "board": _call(cli, ["board", "-s", session, "--json"], environment),
        "members": _call(cli, ["members", "list", session, "--json"], environment),
        "flow": _call(cli, ["flow", "-s", session, "--json"], environment),
    }
    catalog = _call(cli, ["role", "list", "--json"], environment).get("roles")
    assignments = _call(cli, ["role", "assignments", "-s", session, "--json"], environment).get("assignments")
    if not isinstance(catalog, list) or not isinstance(assignments, list):
        raise ExportError("job role catalog or assignments are malformed")
    revisions = {}
    for role in catalog:
        if not isinstance(role, dict) or not isinstance(role.get("name"), str):
            raise ExportError("job role catalog is malformed")
        history = _call(cli, ["role", "history", role["name"], "--json"], environment).get("revisions")
        if not isinstance(history, list) or not history:
            raise ExportError("job role history is missing")
        revisions[role["name"]] = history
    artifacts = _call(cli, ["artifact", "list", "-s", session, "--json"], environment).get("artifacts")
    if not isinstance(artifacts, list):
        raise ExportError("artifact catalog is malformed")
    artifact_root = evidence / "artifacts"
    temporary_root = evidence / f".artifacts-export-{os.getpid()}"
    if temporary_root.exists() or temporary_root.is_symlink():
        raise ExportError("temporary artifact export path already exists")
    temporary_root.mkdir(mode=0o700)
    exported_artifacts = []
    try:
        for item in artifacts:
            if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(item.get("id", ""))):
                raise ExportError("artifact catalog is malformed")
            artifact_id = item["id"]
            path = temporary_root / f"{artifact_id}.bin"
            _download(cli, session, artifact_id, path, environment)
            content = path.read_bytes()
            if len(content) != item.get("size") or hashlib.sha256(content).hexdigest() != item.get("sha256"):
                raise ExportError(f"artifact download failed integrity check: {artifact_id}")
            exported_artifacts.append({**item, "path": f"artifacts/{artifact_id}.bin"})
        if artifact_root.is_symlink() or (artifact_root.exists() and not artifact_root.is_dir()):
            raise ExportError("artifact export path is not an ordinary directory")
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
        os.replace(temporary_root, artifact_root)
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
    _save(evidence / "board-events.jsonl", b"".join((json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n").encode() for row in events))
    _save(evidence / "board-trace.jsonl", b"".join((json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n").encode()
                                                  for row in events if row.get("type") == "message.preflight"))
    _save(evidence / "board-snapshot.json", _json(snapshot))
    _save(evidence / "role-revisions.json", _json({"catalog": catalog, "assignments": assignments,
                                                     "revisions": revisions}))
    _save(evidence / "artifact-manifest.json", _json({"schema": 1, "session": session,
                                                        "artifacts": exported_artifacts}))
    return {"session": session, "events": len(events),
            "preflights": sum(row.get("type") == "message.preflight" for row in events),
            "roles": len(catalog), "assignments": len(assignments), "artifacts": len(exported_artifacts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cli", nargs="+", default=["glassboard"])
    args = parser.parse_args(argv)
    try:
        result = export(args.evidence, args.session, args.cli)
    except (ExportError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Board export incomplete: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
