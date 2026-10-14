-- Verified workspace archives and guarded purge.
-- Archive metadata and purge tombstones deliberately have no foreign key to session: they must
-- survive removal of the live workspace and restoration of a backup that predates the purge.
CREATE TABLE workspace_archive (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  session_handle  TEXT NOT NULL,
  epoch           INTEGER NOT NULL,
  head_seq        INTEGER NOT NULL,
  recovery_id     TEXT NOT NULL,
  created_by      TEXT NOT NULL REFERENCES principal(id),
  created_at      TEXT NOT NULL,
  file_name       TEXT NOT NULL UNIQUE,
  file_size       INTEGER NOT NULL CHECK (file_size > 0),
  sha256          TEXT NOT NULL CHECK (length(sha256) = 64),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64)
);
CREATE INDEX workspace_archive_session ON workspace_archive(session_id, created_at);
CREATE TRIGGER workspace_archive_immutable_u BEFORE UPDATE ON workspace_archive
BEGIN SELECT RAISE(ABORT, 'workspace archive records are immutable'); END;
CREATE TRIGGER workspace_archive_immutable_d BEFORE DELETE ON workspace_archive
BEGIN SELECT RAISE(ABORT, 'workspace archive records are immutable'); END;

CREATE TABLE workspace_purge (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL UNIQUE,
  session_handle  TEXT NOT NULL,
  epoch           INTEGER NOT NULL,
  head_seq        INTEGER NOT NULL,
  archive_id      TEXT NOT NULL,
  archive_sha256  TEXT NOT NULL CHECK (length(archive_sha256) = 64),
  archive_size    INTEGER NOT NULL CHECK (archive_size > 0),
  purged_by       TEXT NOT NULL REFERENCES principal(id),
  purged_at       TEXT NOT NULL,
  journal_n       INTEGER NOT NULL UNIQUE
);
CREATE INDEX workspace_purge_handle ON workspace_purge(session_handle, purged_at);
CREATE TRIGGER workspace_purge_immutable_u BEFORE UPDATE ON workspace_purge
BEGIN SELECT RAISE(ABORT, 'workspace purge tombstones are immutable'); END;
CREATE TRIGGER workspace_purge_immutable_d BEFORE DELETE ON workspace_purge
BEGIN SELECT RAISE(ABORT, 'workspace purge tombstones are immutable'); END;

-- Normally the event and artifact tables remain append-only. A purge operation inserts the
-- session ID into this private guard inside the same write transaction, deletes the complete
-- archived workspace, and removes the guard before commit.
CREATE TABLE workspace_purge_guard (session_id TEXT PRIMARY KEY);
DROP TRIGGER event_immutable_d;
CREATE TRIGGER event_immutable_d BEFORE DELETE ON event
WHEN NOT EXISTS (SELECT 1 FROM workspace_purge_guard g WHERE g.session_id=OLD.session_id)
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
DROP TRIGGER artifact_immutable_delete;
CREATE TRIGGER artifact_immutable_delete BEFORE DELETE ON artifact
WHEN NOT EXISTS (SELECT 1 FROM workspace_purge_guard g WHERE g.session_id=OLD.session_id)
BEGIN SELECT RAISE(ABORT, 'artifact rows are immutable'); END;

-- A purged handle stays reserved so a later workspace cannot be mistaken for the deleted record.
CREATE TRIGGER session_reject_purged_handle BEFORE INSERT ON session
WHEN EXISTS (SELECT 1 FROM workspace_purge p WHERE p.session_handle=NEW.handle)
BEGIN SELECT RAISE(ABORT, 'session handle was purged and remains reserved'); END;
