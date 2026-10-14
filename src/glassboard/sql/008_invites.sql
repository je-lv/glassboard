-- Invites: a human adds one of their agents to a session and gets a short one-time code; the agent
-- redeems it (POST /api/v1/join) for its own token. Only a hash of the code is stored.
CREATE TABLE invite (
  id          TEXT PRIMARY KEY,
  code_sha256 BLOB NOT NULL UNIQUE,
  agent_id    TEXT NOT NULL REFERENCES principal(id),
  session_id  TEXT NOT NULL REFERENCES session(id),
  created_by  TEXT NOT NULL REFERENCES principal(id),
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used_at     TEXT
);
