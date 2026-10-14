-- A security-journal purge may be replayed into a backup that predates the administrator account
-- which performed it. Preserve the actor ID as historical audit data without requiring that older
-- database to contain the principal row.
DROP TRIGGER workspace_purge_immutable_u;
DROP TRIGGER workspace_purge_immutable_d;
DROP TRIGGER session_reject_purged_handle;
DROP INDEX workspace_purge_handle;
ALTER TABLE workspace_purge RENAME TO workspace_purge_with_actor_fk;

CREATE TABLE workspace_purge (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL UNIQUE,
  session_handle  TEXT NOT NULL,
  epoch           INTEGER NOT NULL,
  head_seq        INTEGER NOT NULL,
  archive_id      TEXT NOT NULL,
  archive_sha256  TEXT NOT NULL CHECK (length(archive_sha256) = 64),
  archive_size    INTEGER NOT NULL CHECK (archive_size > 0),
  purged_by       TEXT NOT NULL,
  purged_at       TEXT NOT NULL,
  journal_n       INTEGER NOT NULL UNIQUE
);
INSERT INTO workspace_purge SELECT * FROM workspace_purge_with_actor_fk;
DROP TABLE workspace_purge_with_actor_fk;
CREATE INDEX workspace_purge_handle ON workspace_purge(session_handle, purged_at);
CREATE TRIGGER workspace_purge_immutable_u BEFORE UPDATE ON workspace_purge
BEGIN SELECT RAISE(ABORT, 'workspace purge tombstones are immutable'); END;
CREATE TRIGGER workspace_purge_immutable_d BEFORE DELETE ON workspace_purge
BEGIN SELECT RAISE(ABORT, 'workspace purge tombstones are immutable'); END;
CREATE TRIGGER session_reject_purged_handle BEFORE INSERT ON session
WHEN EXISTS (SELECT 1 FROM workspace_purge p WHERE p.session_handle=NEW.handle)
BEGIN SELECT RAISE(ABORT, 'session handle was purged and remains reserved'); END;
