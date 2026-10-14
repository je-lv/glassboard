"""First-submission snapshots include work that agents did not commit."""
from pathlib import Path
import tarfile

import pytest

from glassboard.run_snapshot import snapshot


def test_snapshot_includes_untracked_files_and_git_metadata(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / "untracked.txt").write_text("agent work")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    result = snapshot(project, private)
    assert result["bytes"] > 0
    with tarfile.open(private / "project-first-submission.tar") as archive:
        assert archive.extractfile("project/untracked.txt").read() == b"agent work"
        assert archive.extractfile("project/.git/HEAD").read() == b"ref: refs/heads/main\n"
    assert (private / "project-first-submission.tar").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        snapshot(project, private)


def test_snapshot_refuses_evidence_inside_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    evidence = project / "private"
    evidence.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="outside"):
        snapshot(project, evidence)
