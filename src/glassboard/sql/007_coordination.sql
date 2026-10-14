-- Coordination: advisory resource leases, and a reviewer per task.
--   lease: one holder per resource per session. Advisory: the board cannot stop work done outside
--          it. It expires when its holder has been idle longer than the session's staleness threshold.
CREATE TABLE lease (
  session_id TEXT NOT NULL REFERENCES session(id),
  resource   TEXT NOT NULL,
  holder_id  TEXT NOT NULL REFERENCES principal(id),
  note       TEXT,
  taken_at   TEXT NOT NULL,
  seq        INTEGER NOT NULL,          -- the lease.taken event
  PRIMARY KEY (session_id, resource)
);
ALTER TABLE task ADD COLUMN reviewer_id TEXT REFERENCES principal(id);
