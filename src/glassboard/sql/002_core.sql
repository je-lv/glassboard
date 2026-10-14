-- 002: the rest of the data model, on top of 001_b0a (meta, principal, credential, audit,
-- which already match it column for column). Additions:
--   * checkpoint.recovery_id: recovery period the checkpoint was saved in.
--   * the `board` system principal (principal kind 'system'; author of task.stale and system.restored).
-- Revocations are recorded in security.journal, which lives outside the database.

CREATE TABLE web_session (
  id_sha256    BLOB PRIMARY KEY,
  principal_id TEXT NOT NULL REFERENCES principal(id),
  csrf_token   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT
);

CREATE TABLE session (
  id               TEXT PRIMARY KEY,
  handle           TEXT NOT NULL UNIQUE,
  title            TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('active','paused','archived')),
  recovery_id      TEXT NOT NULL,                 -- fresh random value at creation and at EVERY restore
  epoch            INTEGER NOT NULL DEFAULT 1,    -- display counter only; never used for enforcement
  head_seq         INTEGER NOT NULL DEFAULT 0,
  next_task_num    INTEGER NOT NULL DEFAULT 1,
  stale_after_s    INTEGER NOT NULL DEFAULT 1800 CHECK (stale_after_s BETWEEN 300 AND 14400),
  protocol_version TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE member (
  session_id       TEXT NOT NULL REFERENCES session(id),
  principal_id     TEXT NOT NULL REFERENCES principal(id),
  role             TEXT NOT NULL CHECK (role IN ('coordinator','member','observer')),
  joined_seq       INTEGER NOT NULL,
  cursor_seq       INTEGER NOT NULL,              -- cumulative ack
  delivered_seq    INTEGER NOT NULL,              -- highest boundary delivered to the current consumer
  consumer_gen     INTEGER NOT NULL DEFAULT 0,
  consumer_secret  TEXT,                          -- NULL = no consumer attached
  consumer_runtime TEXT,                          -- label of attached runtime (validated)
  consumer_since   TEXT,
  joined_at        TEXT NOT NULL,
  left_at          TEXT,
  last_seen_at     TEXT,
  PRIMARY KEY (session_id, principal_id),
  CHECK (cursor_seq <= delivered_seq)
);

CREATE TABLE event (
  session_id  TEXT NOT NULL REFERENCES session(id),
  seq         INTEGER NOT NULL,
  id          TEXT NOT NULL UNIQUE,
  recovery_id TEXT NOT NULL,          -- recovery period in which this event committed
  epoch       INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  actor_id    TEXT NOT NULL REFERENCES principal(id),
  runtime     TEXT,                   -- validated label ^[a-z0-9-]{1,16}$
  type        TEXT NOT NULL,
  broadcast   INTEGER NOT NULL DEFAULT 0,
  task_num    INTEGER,
  reply_to    INTEGER,
  body        TEXT,
  data        TEXT,                   -- JSON structured fields
  op_id       TEXT,
  req_digest  BLOB,
  receipt     TEXT,                   -- exact response JSON at commit; immutable; never exported
  PRIMARY KEY (session_id, seq),
  UNIQUE (session_id, actor_id, op_id)
);
CREATE INDEX event_task  ON event(session_id, task_num, seq);
CREATE INDEX event_reply ON event(session_id, reply_to, type);
CREATE TRIGGER event_immutable_u BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER event_immutable_d BEFORE DELETE ON event BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;

CREATE TABLE event_recipient (
  session_id   TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principal(id),
  PRIMARY KEY (principal_id, session_id, seq),
  FOREIGN KEY (session_id, seq) REFERENCES event(session_id, seq)
);

CREATE TABLE task (
  session_id         TEXT NOT NULL REFERENCES session(id),
  num                INTEGER NOT NULL,
  id                 TEXT NOT NULL UNIQUE,
  title              TEXT NOT NULL,
  scope              TEXT NOT NULL,
  deliverable        TEXT NOT NULL,
  assignee_id        TEXT REFERENCES principal(id),
  status             TEXT NOT NULL CHECK (status IN ('open','in_progress','blocked','review','done','cancelled')),
  version            INTEGER NOT NULL DEFAULT 1,
  gen                INTEGER NOT NULL DEFAULT 0,
  fence_secret       TEXT,               -- NULL = no valid fence (also after restore)
  claimant_id        TEXT REFERENCES principal(id),
  claimant_runtime   TEXT,
  last_activity_at   TEXT,               -- updated ONLY by successful fenced ops
  stale_after_s      INTEGER CHECK (stale_after_s IS NULL OR stale_after_s BETWEEN 300 AND 14400),
  stale_notified_for TEXT,               -- last_activity_at value already reported stale
  summary            TEXT,
  created_by         TEXT NOT NULL REFERENCES principal(id),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (session_id, num)
);

CREATE TABLE checkpoint (
  session_id    TEXT NOT NULL REFERENCES session(id),
  principal_id  TEXT NOT NULL REFERENCES principal(id),
  n             INTEGER NOT NULL,
  id            TEXT NOT NULL UNIQUE,
  seq           INTEGER NOT NULL,
  recovery_id   TEXT NOT NULL,           -- session.recovery_id at save time
  runtime       TEXT,
  summary       TEXT NOT NULL,
  next_action   TEXT NOT NULL,
  blockers      TEXT,
  task_nums     TEXT NOT NULL DEFAULT '[]',
  refs          TEXT NOT NULL DEFAULT '[]',
  last_seen_seq INTEGER NOT NULL,
  body          TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (session_id, principal_id, n)
);

-- The `board` system principal. Fixed ID (ids.BOARD_PRINCIPAL_ID); the handle is thereby reserved.
INSERT INTO principal (id, handle, kind, owner_id, display_name, is_admin, active, created_at)
VALUES ('00000000000000000000000000', 'board', 'system', NULL, 'board', 0, 1,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
