"""Backup and restore.

Backup, safe while the server runs:
  1. SQLite online backup API into a new file (never a copy of the live file: the WAL may hold
     committed pages);
  2. in the copy, `meta.backup_taken_at` and `meta.backup_head_seqs` (head seq of every session);
  3. `PRAGMA integrity_check` on the copy;
  4. a copy of security.journal next to it (`<out>.journal`), for off-host storage;
  5. encryption with `age` to the given recipients; the plaintext temporary file is removed.
     Plaintext backups need an explicit `--no-encrypt`: backups hold token hashes, and receipts that
     can contain fences and consumer tokens.

Restore, on a STOPPED instance (the server holds a shared lock while serving; restore needs it
exclusively):
  1. decrypt if needed, `PRAGMA integrity_check`, copy into place (old WAL/SHM removed);
  2. migrate if the backup is older; replay security.journal from the backup's watermark,
     without per-entry events: the restore event below covers them; revoke every
     web session;
  3. per session, one transaction: fresh 80-bit `recovery_id`, `epoch + 1`; non-archived sessions
     become `paused`, archived ones stay archived; every in_progress/blocked task: `gen + 1`,
     fence cleared (claimant and status kept); every member: consumer cleared, `consumer_gen + 1`,
     `delivered_seq := cursor_seq`; exactly one `system.restored` event by `board`;
  4. `meta.last_restore` and one audit row.
After a restore any coordinator may resume a session.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

from . import db, events, ids, journal

AGE_HEADER = b"age-encryption.org/v1"


def serving_lock_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), ".glassboard-serving.lock")


@contextlib.contextmanager
def serving(db_path: str):
    """Held (shared) by a running server for its lifetime."""
    fd = os.open(serving_lock_path(db_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class RestoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _age(args: list[str]) -> None:
    exe = os.environ.get("GLASSBOARD_AGE", "age")
    try:
        subprocess.run([exe, *args], check=True, capture_output=True, timeout=600)
    except FileNotFoundError:
        raise RestoreError("the `age` tool is not installed (or set GLASSBOARD_AGE); see docs/deploy.md, \"Backups and restore\"") from None
    except subprocess.CalledProcessError as e:
        raise RestoreError(f"age failed: {e.stderr.decode(errors='replace')[:300]}") from None


def _private_tmp(near: str, suffix: str) -> str:
    path = f"{near}.{suffix}.{os.getpid()}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return path


def backup(database: db.Database, out: str, journal_path: str, recipients: list[str], no_encrypt: bool) -> dict:
    if not recipients and not no_encrypt:
        raise RestoreError("backups are encrypted: pass --age-recipient (or --no-encrypt to write plaintext)")
    if os.path.exists(out):
        raise RestoreError(f"{out} already exists")
    plain = _private_tmp(out, "plain") if recipients else out
    if not recipients:
        open(plain, "wb").close()
        os.chmod(plain, 0o600)
    try:
        dst = sqlite3.connect(plain)
        with database.read() as src:
            heads = {r["id"]: r["head_seq"] for r in src.execute("SELECT id, head_seq FROM session")}
        src_conn = sqlite3.connect(database.path)
        try:
            src_conn.backup(dst)                     # online backup API: consistent, includes WAL pages
        finally:
            src_conn.close()
        taken = _now()
        dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('backup_taken_at', ?)", (taken,))
        dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('backup_head_seqs', ?)", (json.dumps(heads),))
        dst.commit()
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close()
        if ok != "ok":
            raise RestoreError(f"integrity_check failed on the backup copy: {ok}")
        jcopy = out + ".journal"
        if os.path.exists(journal_path):
            shutil.copyfile(journal_path, jcopy)
            os.chmod(jcopy, 0o600)
        if recipients:
            args = []
            for r in recipients:
                args += ["-r", r]
            _age([*args, "-o", out, plain])
            os.chmod(out, 0o600)
    finally:
        if recipients and os.path.exists(plain):
            os.remove(plain)
    with database.write() as c:                     # instance-level: one audit row, no event
        c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                  (ids.new_ulid(), db.utcnow(), ids.BOARD_PRINCIPAL_ID, "backup", None,
                   json.dumps({"taken_at": taken, "encrypted": bool(recipients), "sessions": len(heads)})))
    return {"out": out, "taken_at": taken, "sessions": len(heads), "encrypted": bool(recipients),
            "journal_copy": jcopy if os.path.exists(journal_path) else None}


def restore(db_path: str, backup_path: str, journal_path: str, identity: str | None = None,
            journal_source: str | None = None) -> dict:
    lock_fd = os.open(serving_lock_path(db_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RestoreError("the board is running; stop the service before restoring") from None
        if journal_source and not os.path.isfile(journal_source):
            # Checked before anything changes. A backup taken before any access reduction has no
            # journal, so it has no `<backup>.journal` copy either.
            raise RestoreError("the journal copy given with --journal-from does not exist "
                               "(a backup taken before any access reduction has none; omit the option)")
        if not os.path.isfile(backup_path):
            raise RestoreError("the backup file does not exist")
        with open(backup_path, "rb") as f:
            encrypted = f.read(len(AGE_HEADER)) == AGE_HEADER
        src = backup_path
        tmp = None
        if encrypted:
            if not identity:
                raise RestoreError("this backup is encrypted: pass --age-identity")
            tmp = _private_tmp(db_path, "restore")
            os.remove(tmp)
            _age(["-d", "-i", identity, "-o", tmp, backup_path])
            src = tmp
        try:
            try:
                chk = sqlite3.connect(src)
                ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
                meta = dict(chk.execute("SELECT key, value FROM meta").fetchall()) if ok == "ok" else {}
                chk.close()
            except sqlite3.DatabaseError as e:             # not SQLite at all, or unreadable
                raise RestoreError(f"not a usable SQLite database: {e}") from None
            if ok != "ok":
                raise RestoreError(f"integrity_check failed: {ok}")
            if "backup_taken_at" not in meta:
                raise RestoreError("not a Glassboard backup (no backup_taken_at)")
            if journal_source:                          # after a total host loss: the off-host journal copy
                shutil.copyfile(journal_source, journal_path)
                os.chmod(journal_path, 0o600)
            staged = _private_tmp(db_path, "staged")
            shutil.copyfile(src, staged)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    os.remove(db_path + suffix)
            os.replace(staged, db_path)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return _after_copy(db_path, journal_path, meta)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _after_copy(db_path: str, journal_path: str, meta: dict) -> dict:
    database = db.Database(db_path)
    try:
        database.migrate()
        applied = journal.replay(database, journal_path, emit_events=False)       # re-apply access reductions
        taken, heads = meta["backup_taken_at"], json.loads(meta.get("backup_head_seqs", "{}"))
        report = []
        with database.write() as c:
            c.execute("UPDATE web_session SET revoked_at=? WHERE revoked_at IS NULL", (db.utcnow(),))
            # A password changed after the backup cannot be recovered from the access-reduction
            # journal. Require local re-bootstrap so an older password never becomes valid again.
            c.execute("DELETE FROM admin_password")
        with database.read() as c:
            sids = [r["id"] for r in c.execute("SELECT id FROM session ORDER BY handle")]
        for sid in sids:
            with database.write() as c:
                s = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
                archived = s["state"] == "archived"
                epoch = s["epoch"] + 1
                c.execute("UPDATE session SET recovery_id=?, epoch=?, state=?, updated_at=? WHERE id=?",
                          (ids.new_random_b32(16), epoch, "archived" if archived else "paused", db.utcnow(), sid))
                c.execute("UPDATE task SET gen=gen+1, fence_secret=NULL, updated_at=? WHERE session_id=? "
                          "AND status IN ('in_progress','blocked')", (db.utcnow(), sid))
                c.execute("UPDATE member SET consumer_secret=NULL, consumer_runtime=NULL, consumer_since=NULL, "
                          "consumer_gen=consumer_gen+1, delivered_seq=cursor_seq WHERE session_id=?", (sid,))
                head = heads.get(sid, s["head_seq"])
                body = (f"Restored to #{head}. Anything committed after the backup time is gone.\n"
                        "All fences and inbox consumers are invalid. "
                        + ("The session stays archived." if archived else "Session paused until a coordinator resumes."))
                seq, _ = events.append(c, sid, ids.BOARD_PRINCIPAL_ID, "system.restored", broadcast=True, body=body,
                                       data={"backup_taken_at": taken, "backup_head_seq": head, "epoch": epoch})
                report.append({"session": s["handle"], "epoch": epoch, "state": "archived" if archived else "paused",
                               "backup_head": head, "restored_event": seq})
        with database.write() as c:
            info = {"backup_taken_at": taken, "restored_at": _now(), "journal_entries_reapplied": applied,
                    "sessions": len(report)}
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('last_restore', ?)", (json.dumps(info),))
            c.execute("INSERT INTO audit(id,ts,actor_id,action,target,data) VALUES (?,?,?,?,?,?)",
                      (ids.new_ulid(), db.utcnow(), ids.BOARD_PRINCIPAL_ID, "restore", None, json.dumps(info)))
        return {**info, "per_session": report}
    finally:
        database.close()
