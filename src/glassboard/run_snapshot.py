"""Preserve an exact first-submission working tree, including untracked files."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tarfile

from .run_evidence import _hash


def snapshot(project: Path, evidence: Path) -> dict:
    if project.is_symlink() or not project.is_dir() or evidence.is_symlink() or not evidence.is_dir():
        raise ValueError("project and evidence must be ordinary directories")
    project, evidence = project.resolve(), evidence.resolve()
    if evidence == project or evidence.is_relative_to(project):
        raise ValueError("evidence must be outside the project tree")
    if evidence.stat().st_mode & 0o077:
        raise ValueError("evidence directory must be mode 0700")
    target = evidence / "project-first-submission.tar"
    if target.exists():
        raise FileExistsError("first submission has already been captured")
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("xb") as raw:
        os.chmod(temporary, 0o600)
        with tarfile.open(fileobj=raw, mode="w", dereference=False) as archive:
            archive.add(project, arcname="project", recursive=True)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, target)
    return {"path": str(target), "bytes": target.stat().st_size, "sha256": _hash(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.project, args.evidence)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"Snapshot incomplete: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Captured {result['bytes']} bytes at {result['path']} (SHA-256 {result['sha256']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
