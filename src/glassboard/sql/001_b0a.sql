-- Initial subset of the schema. Column names match the full data model, so later migrations
-- extend it rather than rewrite it.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS principal (
  id           TEXT PRIMARY KEY,
  handle       TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL CHECK (kind IN ('human','agent','client','system')),
  owner_id     TEXT REFERENCES principal(id),
  display_name TEXT NOT NULL,
  is_admin     INTEGER NOT NULL DEFAULT 0,
  client_info  TEXT,
  active       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  CHECK ((kind IN ('human','system')) = (owner_id IS NULL))
);

CREATE TABLE IF NOT EXISTS credential (
  id            TEXT PRIMARY KEY,
  principal_id  TEXT NOT NULL REFERENCES principal(id),
  secret_sha256 BLOB NOT NULL,
  scope         TEXT NOT NULL CHECK (scope IN ('full','read')),
  label         TEXT,
  created_at    TEXT NOT NULL,
  expires_at    TEXT,
  revoked_at    TEXT,
  last_used_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT, data TEXT, op_id TEXT, receipt TEXT,
  UNIQUE (actor_id, op_id)
);
