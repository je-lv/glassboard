"""Capture one external agent round for a private, auditable rehearsal.

The client must be configured to emit JSON lines on stdout. This module records
the exact bytes it exposes; it cannot obtain private provider reasoning.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_status(path: Path) -> tuple[int, int]:
    valid = invalid = 0
    with path.open("rb") as stream:
        for line in stream:
            try:
                if not isinstance(json.loads(line), dict):
                    raise ValueError("JSON event is not an object")
                valid += 1
            except (UnicodeDecodeError, ValueError):
                invalid += 1
    return valid, invalid


def _stop_process_group(process: subprocess.Popen) -> None:
    """Stop a captured client and descendants without leaving an orphaned agent run."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def _interrupt_capture(_signum, _frame) -> None:
    """Turn supervisor termination into the same cleanup path as Ctrl+C."""
    raise KeyboardInterrupt


def capture(evidence: Path, agent: str, round_number: int, prompt: Path,
            cwd: Path, command: list[str], *, prompt_stdin: bool = False,
            native_files: list[Path] | None = None,
            native_globs: list[str] | None = None,
            native_unavailable_reason: str | None = None) -> dict:
    """Create an exclusive evidence directory and run exactly one client process."""
    if not agent or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in agent):
        raise ValueError("agent must contain only lowercase letters, digits, hyphen or underscore")
    if round_number < 1 or not command:
        raise ValueError("a positive round and a command are required")
    if not prompt.is_file() or not cwd.is_dir():
        raise ValueError("prompt file and working directory must exist")
    if (native_files or native_globs) and native_unavailable_reason:
        raise ValueError("declare native files or explain their absence, not both")
    if evidence.is_symlink():
        raise ValueError("evidence directory cannot be a symlink")
    evidence = evidence.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(evidence, 0o700)
    round_dir = evidence / f"{agent}-round{round_number:02}"
    round_dir.mkdir(mode=0o700)
    prompt_copy = round_dir / "prompt.txt"
    with prompt.open("rb") as source, prompt_copy.open("xb") as destination:
        os.chmod(prompt_copy, 0o600)
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    stdout_path = round_dir / "stdout.jsonl"
    stderr_path = round_dir / "stderr.bin"
    record_path = round_dir / "record.json"
    record = {
        "schema": 1, "agent": agent, "round": round_number,
        "started_at": _utc(), "status": "running", "cwd": str(cwd.resolve()),
        "argv": command, "prompt_stdin": prompt_stdin,
        "prompt_sha256": _hash(prompt_copy), "native_sources": [str(p) for p in native_files or []],
        "native_globs": native_globs or [],
        "native_unavailable_reason": native_unavailable_reason,
        "files": {"prompt": "prompt.txt", "stdout": "stdout.jsonl", "stderr": "stderr.bin"},
    }
    _write_json(record_path, record)
    process = None
    previous_sigterm = signal.signal(signal.SIGTERM, _interrupt_capture)
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            with prompt_copy.open("rb") if prompt_stdin else open(os.devnull, "rb") as stdin:
                process = subprocess.Popen(command, cwd=cwd, stdin=stdin,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                record["pid"] = process.pid
                _write_json(record_path, record)
                record["exit_code"] = process.wait()
    except BaseException as exc:
        if process is not None:
            _stop_process_group(process)
            record["exit_code"] = process.returncode
        record["status"] = "capture_error"
        record["error_type"] = type(exc).__name__
        record["ended_at"] = _utc()
        if stdout_path.exists():
            valid, invalid = _jsonl_status(stdout_path)
            record.update(stdout_sha256=_hash(stdout_path), json_events=valid,
                          invalid_json_lines=invalid)
        if stderr_path.exists():
            record["stderr_sha256"] = _hash(stderr_path)
        _write_json(record_path, record)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    valid, invalid = _jsonl_status(stdout_path)
    native = []
    native_gap = False
    discovered = list(native_files or [])
    for pattern in native_globs or []:
        matches = sorted(glob.glob(pattern, recursive=True))
        if not matches:
            native_gap = True
        discovered.extend(Path(match) for match in matches)
    discovered = list(dict.fromkeys(discovered))
    for index, source in enumerate(discovered, 1):
        try:
            if source.is_symlink() or not source.is_file():
                native.append({"source": str(source), "status": "missing_or_symlink"})
                native_gap = True
                continue
            target = round_dir / f"native-{index:02}{source.suffix}"
            with source.open("rb") as origin, target.open("xb") as destination:
                os.chmod(target, 0o600)
                shutil.copyfileobj(origin, destination)
                destination.flush()
                os.fsync(destination.fileno())
            native.append({"source": str(source), "copy": target.name,
                           "sha256": _hash(target), "status": "copied"})
        except OSError as exc:
            native.append({"source": str(source), "status": "copy_error", "error_type": type(exc).__name__})
            native_gap = True
    record.update({
        "ended_at": _utc(), "stdout_sha256": _hash(stdout_path),
        "stderr_sha256": _hash(stderr_path), "json_events": valid,
        "invalid_json_lines": invalid, "native_files": native,
        "status": "complete" if record["exit_code"] == 0 and valid > 0 and invalid == 0 and not native_gap
                  and (discovered or (native_unavailable_reason and native_unavailable_reason.strip()))
                  else "incomplete",
    })
    _write_json(record_path, record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--prompt-stdin", action="store_true")
    parser.add_argument("--native-file", type=Path, action="append", default=[])
    parser.add_argument("--native-glob", action="append", default=[], help="glob evaluated after the client exits")
    parser.add_argument("--native-unavailable-reason", help="why this client exposes no native history")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        record = capture(args.evidence, args.agent, args.round, args.prompt,
                         args.cwd, command, prompt_stdin=args.prompt_stdin,
                         native_files=args.native_file,
                         native_globs=args.native_glob,
                         native_unavailable_reason=args.native_unavailable_reason)
    except KeyboardInterrupt:
        print("capture interrupted; partial evidence record preserved", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        parser.exit(2, f"capture failed: {type(exc).__name__}\n")
    print(json.dumps({"status": record["status"], "agent": record["agent"],
                      "round": record["round"], "record": str(args.evidence / f"{args.agent}-round{args.round:02}" / "record.json")}))
    return 0 if record["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
