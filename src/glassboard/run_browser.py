"""Admin-only, read-only projections of explicitly configured run-evidence roots."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import render

ROUND_RE = re.compile(r"^(?P<agent>[a-z0-9-]{1,64})-round(?P<round>[0-9]{2,6})$")
NATIVE_RE = re.compile(r"^native-[0-9]{2,6}\.[A-Za-z0-9._-]{1,16}$")
MAX_FILE = 16 * 1024 * 1024
MAX_EVENTS = 500


def configured(value: str, label: str = "GLASSBOARD_ADMIN_RUN_EVIDENCE_ROOTS") -> list[str]:
    try:
        roots = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON list of absolute directory paths") from exc
    if not isinstance(roots, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
        raise ValueError(f"{label} must be a JSON list of absolute directory paths")
    return roots


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def list_rounds(roots: list[str]) -> tuple[list[dict], list[int]]:
    rounds, unavailable = [], []
    for index, raw in enumerate(roots):
        root = Path(raw)
        try:
            children = list(root.iterdir()) if root.is_dir() and not root.is_symlink() else []
        except OSError:
            unavailable.append(index)
            continue
        if not children and not root.is_dir():
            unavailable.append(index)
        for child in children:
            match = ROUND_RE.fullmatch(child.name)
            if match is None or child.is_symlink() or not child.is_dir():
                continue
            record = _json(child / "record.json")
            if not record:
                continue
            rounds.append({
                "root": index, "name": child.name, "agent": render.r2(str(record.get("agent") or match["agent"])),
                "round": record.get("round", int(match["round"])), "status": render.r2(str(record.get("status", "unknown"))),
                "started_at": render.r2(str(record.get("started_at", ""))),
                "ended_at": render.r2(str(record.get("ended_at", ""))), "exit_code": record.get("exit_code"),
                "events": record.get("json_events"), "invalid": record.get("invalid_json_lines"),
                "native": len(record.get("native_files") or ()),
            })
    rounds.sort(key=lambda x: (x["started_at"], x["agent"], x["round"]), reverse=True)
    return rounds, unavailable


def _round_dir(roots: list[str], index: int, name: str) -> Path:
    if index < 0 or index >= len(roots) or ROUND_RE.fullmatch(name) is None:
        raise LookupError
    root = Path(roots[index]).resolve()
    child = root / name
    if child.is_symlink() or not child.is_dir() or child.resolve().parent != root:
        raise LookupError
    return child


def _classify(value) -> str:
    text = " ".join(str(x).lower() for x in (
        value.get("type", ""), value.get("role", ""),
        (value.get("item") or {}).get("type", "") if isinstance(value.get("item"), dict) else ""))
    if "reason" in text or "thinking" in text:
        return "provider reasoning output"
    if "tool" in text or "command" in text or "function" in text:
        return "tool activity"
    if "assistant" in text or "agent_message" in text:
        return "verbatim model output"
    return "provider event"


def detail(roots: list[str], index: int, name: str, *, before: int | None = None) -> dict:
    directory = _round_dir(roots, index, name)
    record = _json(directory / "record.json")
    if not record:
        raise LookupError
    prompt = ""
    prompt_path = directory / "prompt.txt"
    if prompt_path.is_file() and not prompt_path.is_symlink() and prompt_path.stat().st_size <= MAX_FILE:
        try:
            prompt = render.r2(prompt_path.read_text(encoding="utf-8"), allow_newline=True)
        except (OSError, UnicodeError):
            prompt = ""
    events = []
    stdout = directory / "stdout.jsonl"
    if stdout.is_file() and not stdout.is_symlink() and stdout.stat().st_size <= MAX_FILE:
        try:
            lines = stdout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        end = min(before if before is not None else len(lines), len(lines))
        start = max(0, end - MAX_EVENTS)
        for number, line in enumerate(lines[start:end], start + 1):
            try:
                value = json.loads(line)
                shown = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                label = _classify(value) if isinstance(value, dict) else "provider event"
            except json.JSONDecodeError:
                shown, label = line, "malformed provider output"
            events.append({"line": number, "label": label,
                           "text": render.r2(shown, allow_newline=True)})
        older_before = start if start else None
    else:
        older_before = None
    native = []
    for item in record.get("native_files") or ():
        if isinstance(item, dict):
            copy = item.get("copy") if isinstance(item.get("copy"), str) else None
            native.append({"name": render.r2(str(copy or item.get("source") or "native stream")),
                           "file": copy if copy and NATIVE_RE.fullmatch(copy) else None,
                           "status": render.r2(str(item.get("status", "unknown")))})
    files = []
    for filename, label in (("prompt.txt", "launch prompt"), ("stdout.jsonl", "provider stdout"),
                            ("stderr.bin", "provider stderr"), ("record.json", "capture record")):
        path = directory / filename
        if path.is_file() and not path.is_symlink():
            files.append({"name": filename, "label": label, "bytes": path.stat().st_size})
    safe_record = render.r2(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), allow_newline=True)
    return {"root": index, "name": name, "record": record, "record_text": safe_record, "prompt": prompt,
            "events": events, "older_before": older_before, "native": native,
            "files": files,
            "reasoning_note": ("Native client streams are listed below; their contents depend on the provider." if native else
                               render.r2(str(record.get("native_unavailable_reason") or
                                             "The provider did not expose a separate private-reasoning stream.")))}


def file_detail(roots: list[str], index: int, name: str, filename: str,
                *, before: int | None = None) -> dict:
    """Read one explicitly named capture file without permitting arbitrary paths."""
    if filename not in {"prompt.txt", "stdout.jsonl", "stderr.bin", "record.json"} \
            and NATIVE_RE.fullmatch(filename) is None:
        raise LookupError
    directory = _round_dir(roots, index, name)
    path = directory / filename
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise LookupError
    size = path.stat().st_size
    if size > MAX_FILE:
        return {"root": index, "name": name, "filename": filename, "bytes": size,
                "lines": [], "older_before": None, "too_large": True}
    try:
        raw = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise LookupError from exc
    all_lines = raw.splitlines() or ([raw] if raw else [])
    end = min(before if before is not None else len(all_lines), len(all_lines))
    start = max(0, end - MAX_EVENTS)
    lines = [{"number": number, "text": render.r2(line, allow_newline=True)}
             for number, line in enumerate(all_lines[start:end], start + 1)]
    return {"root": index, "name": name, "filename": filename, "bytes": size,
            "lines": lines, "older_before": start if start else None, "too_large": False}
