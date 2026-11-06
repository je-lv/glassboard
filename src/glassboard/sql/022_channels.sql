-- Topic channels inside a session (item B55, ERRATA E43). A channel decides whose inbox a message
-- reaches and who is woken; it never hides anything: every event stays in the one session log that
-- every member can read. Members choose follow, mentions (only messages addressed to them) or mute.
CREATE TABLE channel (
  session_id  TEXT NOT NULL REFERENCES session(id),
  name        TEXT NOT NULL,
  topic       TEXT,
  post_policy TEXT NOT NULL DEFAULT 'all' CHECK (post_policy IN ('all', 'listed')),
  created_by  TEXT NOT NULL REFERENCES principal(id),
  created_at  TEXT NOT NULL,
  archived_at TEXT,
  PRIMARY KEY (session_id, name)
);
CREATE TABLE channel_member (
  session_id   TEXT NOT NULL,
  name         TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principal(id),
  mode         TEXT NOT NULL DEFAULT 'follow' CHECK (mode IN ('follow', 'mentions', 'mute')),
  can_post     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (session_id, name, principal_id),
  FOREIGN KEY (session_id, name) REFERENCES channel(session_id, name)
);
ALTER TABLE event ADD COLUMN channel TEXT;
CREATE INDEX event_channel ON event(session_id, channel, seq) WHERE channel IS NOT NULL;
