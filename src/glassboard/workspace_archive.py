"""Verified archives and irreversible cleanup for completed workspaces.

Artifacts and events remain immutable during normal operation. An administrator may purge only an
archived workspace, only after Glassboard has created and re-verified a complete archive. The
security journal records the purge before the database transaction commits, so restoring an older
backup cannot resurrect the deleted workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile

from . import db, ids, inbox, journal
from .textviews import view_lines

FORMAT = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


class WorkspaceArchiveError(ValueError):
    """Archive creation, verification or purge was refused."""


def archive_directory(database_path: str) -> Path:
    return Path(database_path).resolve().parent / "workspace-archives"


def human_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def _jsonl(values) -> bytes:
    return b"".join((json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    for value in values)


def _public_row(row, *, remove: tuple[str, ...] = ()) -> dict:
    value = dict(row)
    for key in remove:
        value.pop(key, None)
    return value


def _snapshot(conn: sqlite3.Connection, handle: str, archive_id: str, created_at: str) -> tuple[dict, dict[str, bytes]]:
    sess = conn.execute("SELECT * FROM session WHERE handle=?", (handle,)).fetchone()
    if sess is None:
        raise WorkspaceArchiveError("No such workspace.")
    if sess["state"] != "archived":
        raise WorkspaceArchiveError("Archive the workspace before creating its cleanup archive.")
    sid = sess["id"]
    principals = {row["id"]: row["handle"] for row in conn.execute("SELECT id,handle FROM principal")}

    event_rows = conn.execute("SELECT * FROM event WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
    event_views = inbox.views(conn, event_rows)
    if len(event_views) != sess["head_seq"] or any(row["seq"] != n for n, row in enumerate(event_views, 1)):
        raise WorkspaceArchiveError("The workspace event log is incomplete.")
    timeline = [f"--- glassboard workspace archive · {handle} · archived · epoch {sess['epoch']} · head #{sess['head_seq']} ---"]
    for event in event_views:
        timeline.extend(view_lines(event))

    members = []
    for row in conn.execute("SELECT * FROM member WHERE session_id=? ORDER BY joined_seq,principal_id", (sid,)):
        item = _public_row(row, remove=("consumer_secret",))
        item["principal"] = principals.get(item.pop("principal_id"), "?")
        members.append(item)

    tasks = []
    for row in conn.execute("SELECT * FROM task WHERE session_id=? ORDER BY num", (sid,)):
        item = _public_row(row, remove=("fence_secret",))
        for source, target in (("assignee_id", "assignee"), ("claimant_id", "claimant"),
                               ("reviewer_id", "reviewer"), ("created_by", "created_by")):
            item[target] = principals.get(item.pop(source), None)
        tasks.append(item)

    checkpoints = []
    for row in conn.execute("SELECT * FROM checkpoint WHERE session_id=? ORDER BY principal_id,n", (sid,)):
        item = _public_row(row)
        item["principal"] = principals.get(item.pop("principal_id"), "?")
        checkpoints.append(item)

    leases = []
    for row in conn.execute("SELECT * FROM lease WHERE session_id=? ORDER BY resource", (sid,)):
        item = _public_row(row)
        item["holder"] = principals.get(item.pop("holder_id"), "?")
        leases.append(item)

    invites = []
    for row in conn.execute("SELECT * FROM invite WHERE session_id=? ORDER BY created_at,id", (sid,)):
        item = _public_row(row, remove=("code_sha256",))
        item["agent"] = principals.get(item.pop("agent_id"), "?")
        item["created_by"] = principals.get(item["created_by"], "?")
        invites.append(item)

    role_names = sorted({row["job_role"] for row in members if row.get("job_role")})
    roles = []
    for name in role_names:
        roles.extend(_public_row(row) for row in conn.execute(
            "SELECT r.name,r.version,r.prompt,r.updated_at,p.handle updated_by "
            "FROM role_template_revision r JOIN principal p ON p.id=r.updated_by "
            "WHERE r.name=? ORDER BY r.version", (name,)))

    artifact_manifest = []
    files: dict[str, bytes] = {
        "board-events.jsonl": _jsonl(event_views),
        "board-timeline.md": ("\n".join(timeline) + "\n").encode(),
        "members.jsonl": _jsonl(members),
        "tasks.jsonl": _jsonl(tasks),
        "checkpoints.jsonl": _jsonl(checkpoints),
        "leases.jsonl": _jsonl(leases),
        "invites.jsonl": _jsonl(invites),
        "role-revisions.jsonl": _jsonl(roles),
    }
    for row in conn.execute("SELECT * FROM artifact WHERE session_id=? ORDER BY created_at,id", (sid,)):
        content = bytes(row["content"])
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != row["size"] or digest != row["sha256"]:
            raise WorkspaceArchiveError(f"Artifact {row['id']} failed its stored integrity check.")
        path = f"artifacts/{row['id']}.bin"
        files[path] = content
        artifact_manifest.append({
            "id": row["id"], "path": path, "name": row["name"], "media_type": row["media_type"],
            "description": row["description"], "size": row["size"], "sha256": row["sha256"],
            "purpose": row["purpose"],
            "task": row["task_num"], "created_by": principals.get(row["created_by"], "?"),
            "created_at": row["created_at"], "event_seq": row["event_seq"],
        })
    files["artifacts.json"] = _json({"schema": 1, "artifacts": artifact_manifest})

    session_record = _public_row(sess)
    files["session.json"] = _json(session_record)
    manifest = {
        "schema": FORMAT,
        "archive_id": archive_id,
        "created_at": created_at,
        "session": {"id": sid, "handle": handle, "state": "archived", "epoch": sess["epoch"],
                    "head_seq": sess["head_seq"], "recovery_id": sess["recovery_id"]},
        "counts": {"events": len(event_views), "members": len(members), "tasks": len(tasks),
                   "checkpoints": len(checkpoints), "leases": len(leases), "invites": len(invites),
                   "role_revisions": len(roles), "artifacts": len(artifact_manifest)},
        "entries": [],
    }
    for path in sorted(files):
        content = files[path]
        manifest["entries"].append({"path": path, "size": len(content),
                                    "sha256": hashlib.sha256(content).hexdigest()})
    return manifest, files


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and "\\" not in name and not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


def verify_archive(path: Path, expected: dict | sqlite3.Row | None = None) -> dict:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceArchiveError("The workspace archive is missing or is not an ordinary file.")
    file_size = path.stat().st_size
    if file_size <= 0 or file_size > MAX_ARCHIVE_BYTES:
        raise WorkspaceArchiveError("The workspace archive has an invalid size.")
    file_hash = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            file_hash.update(block)
    digest = file_hash.hexdigest()

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or "manifest.json" not in names or any(not _safe_member(name) for name in names):
                raise WorkspaceArchiveError("The workspace archive has unsafe or duplicate members.")
            info_by_name = {item.filename: item for item in infos}
            if info_by_name["manifest.json"].file_size > MAX_MANIFEST_BYTES:
                raise WorkspaceArchiveError("The workspace archive manifest is too large.")
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict) or manifest.get("schema") != FORMAT:
                raise WorkspaceArchiveError("The workspace archive manifest is invalid.")
            entries = manifest.get("entries")
            if not isinstance(entries, list):
                raise WorkspaceArchiveError("The workspace archive entries are invalid.")
            expected_names = {"manifest.json"}
            total = 0
            for item in entries:
                if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"} or not _safe_member(item.get("path", "")):
                    raise WorkspaceArchiveError("The workspace archive entry metadata is invalid.")
                name, size, sha = item["path"], item["size"], item["sha256"]
                if name in expected_names or name not in info_by_name or not isinstance(size, int) or size < 0 \
                        or not isinstance(sha, str) or len(sha) != 64:
                    raise WorkspaceArchiveError("The workspace archive entry metadata is inconsistent.")
                expected_names.add(name)
                actual_size, actual_hash = 0, hashlib.sha256()
                with archive.open(name) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        actual_size += len(block)
                        total += len(block)
                        if total > MAX_ARCHIVE_BYTES:
                            raise WorkspaceArchiveError("The workspace archive payload is too large.")
                        actual_hash.update(block)
                if actual_size != size or actual_hash.hexdigest() != sha or info_by_name[name].file_size != size:
                    raise WorkspaceArchiveError(f"Workspace archive member {name} failed verification.")
            if set(names) != expected_names:
                raise WorkspaceArchiveError("The workspace archive has unlisted members.")
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, WorkspaceArchiveError):
            raise
        raise WorkspaceArchiveError("The workspace archive could not be verified.") from exc

    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if expected is not None:
        checks = {"archive_id": expected["id"], "session_id": expected["session_id"],
                  "session_handle": expected["session_handle"], "epoch": expected["epoch"],
                  "head_seq": expected["head_seq"], "recovery_id": expected["recovery_id"]}
        session = manifest.get("session") or {}
        actual = {"archive_id": manifest.get("archive_id"), "session_id": session.get("id"),
                  "session_handle": session.get("handle"), "epoch": session.get("epoch"),
                  "head_seq": session.get("head_seq"), "recovery_id": session.get("recovery_id")}
        if actual != checks or file_size != expected["file_size"] or digest != expected["sha256"] \
                or manifest_sha != expected["manifest_sha256"]:
            raise WorkspaceArchiveError("The workspace archive no longer matches its recorded receipt.")
    return {"manifest": manifest, "file_size": file_size, "sha256": digest,
            "manifest_sha256": manifest_sha}


def create_archive(database: db.Database, handle: str, actor_id: str) -> dict:
    archive_id, created_at = ids.new_ulid(), db.utcnow()
    folder = archive_directory(database.path)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise WorkspaceArchiveError("The workspace archive directory is unsafe.")
    os.chmod(folder, 0o700)
    with database.read() as conn:
        manifest, files = _snapshot(conn, handle, archive_id, created_at)
    file_name = f"glassboard-{handle}-e{manifest['session']['epoch']}-h{manifest['session']['head_seq']}-{archive_id}.zip"
    final = folder / file_name
    fd, temporary_name = tempfile.mkstemp(prefix=".workspace-archive-", suffix=".tmp", dir=folder)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                             allowZip64=True) as archive:
            for name in sorted(files):
                archive.writestr(name, files[name])
            archive.writestr("manifest.json", _json(manifest))
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        dirfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        verified = verify_archive(final)
        with database.write() as conn:
            current = conn.execute("SELECT id,state,epoch,head_seq,recovery_id FROM session WHERE handle=?", (handle,)).fetchone()
            if current is None or current["state"] != "archived" or any((
                    current["id"] != manifest["session"]["id"], current["epoch"] != manifest["session"]["epoch"],
                    current["head_seq"] != manifest["session"]["head_seq"],
                    current["recovery_id"] != manifest["session"]["recovery_id"])):
                raise WorkspaceArchiveError("The workspace changed while its archive was being created.")
            conn.execute("INSERT INTO workspace_archive(id,session_id,session_handle,epoch,head_seq,recovery_id,"
                         "created_by,created_at,file_name,file_size,sha256,manifest_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (archive_id, current["id"], handle, current["epoch"], current["head_seq"],
                          current["recovery_id"], actor_id, created_at, file_name, verified["file_size"],
                          verified["sha256"], verified["manifest_sha256"]))
            conn.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                         (ids.new_ulid(), db.utcnow(), actor_id, "workspace.archive.create", handle,
                          json.dumps({"archive_id": archive_id, "head_seq": current["head_seq"],
                                      "file_size": verified["file_size"], "sha256": verified["sha256"]})))
        return {"id": archive_id, "handle": handle, "file_name": file_name, **verified,
                "path": str(final)}
    except BaseException:
        temporary.unlink(missing_ok=True)
        if not _archive_record_exists(database, archive_id):
            final.unlink(missing_ok=True)
        raise


def _archive_record_exists(database: db.Database, archive_id: str) -> bool:
    try:
        with database.read() as conn:
            return conn.execute("SELECT 1 FROM workspace_archive WHERE id=?", (archive_id,)).fetchone() is not None
    except sqlite3.Error:
        return False


def archive_record(database: db.Database, archive_id: str):
    with database.read() as conn:
        row = conn.execute("SELECT * FROM workspace_archive WHERE id=?", (archive_id,)).fetchone()
        return dict(row) if row else None


def archive_path(database: db.Database, row: dict | sqlite3.Row) -> Path:
    name = row["file_name"]
    if not isinstance(name, str) or "/" in name or "\\" in name or name in ("", ".", ".."):
        raise WorkspaceArchiveError("The recorded workspace archive path is invalid.")
    return archive_directory(database.path) / name


def purge_workspace(database: db.Database, journal_path: str, handle: str, archive_id: str,
                    actor_id: str) -> dict:
    row = archive_record(database, archive_id)
    if row is None or row["session_handle"] != handle:
        raise WorkspaceArchiveError("Select a verified archive for this workspace.")
    verified = verify_archive(archive_path(database, row), row)
    with database.write() as conn:
        sess = conn.execute("SELECT * FROM session WHERE handle=?", (handle,)).fetchone()
        current_archive = conn.execute("SELECT * FROM workspace_archive WHERE id=?", (archive_id,)).fetchone()
        if sess is None:
            raise WorkspaceArchiveError("No such workspace.")
        if sess["state"] != "archived":
            raise WorkspaceArchiveError("Only an archived workspace can be purged.")
        if current_archive is None or any((sess["id"] != current_archive["session_id"],
                                           sess["epoch"] != current_archive["epoch"],
                                           sess["head_seq"] != current_archive["head_seq"],
                                           sess["recovery_id"] != current_archive["recovery_id"])):
            raise WorkspaceArchiveError("The workspace no longer matches the selected archive.")
        entry = journal.append(journal_path, "session.purge", session_id=sess["id"],
                               session_handle=handle, archive_id=archive_id,
                               archive_sha256=current_archive["sha256"],
                               archive_size=str(current_archive["file_size"]), head_seq=str(sess["head_seq"]),
                               epoch=str(sess["epoch"]), purged_by=actor_id)
        if not journal.apply_entry(conn, entry):
            raise WorkspaceArchiveError("The workspace could not be purged.")
        conn.execute("INSERT INTO audit(id,ts,actor_id,action,target,data,op_id) VALUES (?,?,?,?,?,?,?)",
                     (ids.new_ulid(), db.utcnow(), actor_id, "workspace.purge", handle,
                      json.dumps({"archive_id": archive_id, "archive_sha256": verified["sha256"],
                                  "archive_size": verified["file_size"], "head_seq": sess["head_seq"],
                                  "journal_n": entry["n"]}), entry["op_id"]))
    return {"handle": handle, "archive_id": archive_id, "sha256": verified["sha256"],
            "file_size": verified["file_size"], "journal_n": entry["n"]}


def storage_view(database: db.Database) -> dict:
    with database.read() as conn:
        workspaces = [dict(row) for row in conn.execute(
            "SELECT s.id,s.handle,s.title,s.state,s.epoch,s.head_seq,s.bytes_used event_bytes,"
            "COUNT(a.id) artifact_count,COALESCE(SUM(a.size),0) artifact_bytes "
            "FROM session s LEFT JOIN artifact a ON a.session_id=s.id "
            "GROUP BY s.id ORDER BY s.created_at DESC")]
        archives = [dict(row) for row in conn.execute(
            "SELECT a.*,p.handle created_by_handle FROM workspace_archive a "
            "JOIN principal p ON p.id=a.created_by ORDER BY a.created_at DESC")]
        purges = [dict(row) for row in conn.execute(
            "SELECT w.*,p.handle purged_by_handle FROM workspace_purge w "
            "JOIN principal p ON p.id=w.purged_by ORDER BY w.purged_at DESC")]
    for item in workspaces:
        item["logical_bytes"] = item["event_bytes"] + item["artifact_bytes"]
        item["event_size"] = human_bytes(item["event_bytes"])
        item["artifact_size"] = human_bytes(item["artifact_bytes"])
        item["logical_size"] = human_bytes(item["logical_bytes"])
        item["archives"] = [a for a in archives if a["session_id"] == item["id"]]
    folder = archive_directory(database.path)
    for item in archives:
        path = archive_path(database, item)
        item["present"] = path.is_file() and not path.is_symlink()
        item["size_label"] = human_bytes(item["file_size"])
        item["sha_short"] = item["sha256"][:16]
        item["purged"] = any(p["archive_id"] == item["id"] for p in purges)
    db_path = Path(database.path)
    files = {"database": db_path, "wal": Path(str(db_path) + "-wal")}
    backup_dir = db_path.resolve().parent / "backups"
    archive_bytes = sum(p.stat().st_size for p in folder.glob("*.zip") if p.is_file() and not p.is_symlink()) if folder.exists() else 0
    backup_bytes = sum(p.stat().st_size for p in backup_dir.iterdir() if p.is_file() and not p.is_symlink()) if backup_dir.exists() else 0
    disk = {name: (path.stat().st_size if path.is_file() else 0) for name, path in files.items()}
    disk.update({"archives": archive_bytes, "backups": backup_bytes})
    disk["total"] = sum(disk.values())
    disk["labels"] = {key: human_bytes(value) for key, value in disk.items() if key != "labels"}
    return {"workspaces": workspaces, "archives": archives, "purges": purges, "disk": disk}
