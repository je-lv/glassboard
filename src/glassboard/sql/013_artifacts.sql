-- Immutable evidence artifacts. Bytes live in SQLite so normal database backups and restores keep
-- the evidence with the session record. Source collaboration remains in Git.
CREATE TABLE artifact (
  session_id   TEXT NOT NULL REFERENCES session(id),
  id           TEXT NOT NULL,
  task_num     INTEGER,
  name         TEXT NOT NULL,
  media_type   TEXT NOT NULL,
  description  TEXT,
  size         INTEGER NOT NULL CHECK (size > 0),
  sha256       TEXT NOT NULL CHECK (length(sha256) = 64),
  content      BLOB NOT NULL,
  created_by   TEXT NOT NULL REFERENCES principal(id),
  created_at   TEXT NOT NULL,
  event_seq    INTEGER NOT NULL,
  PRIMARY KEY (session_id, id),
  FOREIGN KEY (session_id, task_num) REFERENCES task(session_id, num),
  FOREIGN KEY (session_id, event_seq) REFERENCES event(session_id, seq)
);
CREATE INDEX artifact_task ON artifact(session_id, task_num, created_at);
CREATE TRIGGER artifact_immutable_update BEFORE UPDATE ON artifact
BEGIN SELECT RAISE(ABORT, 'artifact rows are immutable'); END;
CREATE TRIGGER artifact_immutable_delete BEFORE DELETE ON artifact
BEGIN SELECT RAISE(ABORT, 'artifact rows are immutable'); END;

