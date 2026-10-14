-- Keep a browser session bound to the credential and scope used at login.
-- Existing sessions lacked that binding and must reauthenticate after this migration.
ALTER TABLE web_session ADD COLUMN credential_id TEXT REFERENCES credential(id);
ALTER TABLE web_session ADD COLUMN scope TEXT NOT NULL DEFAULT 'full' CHECK (scope IN ('full', 'read'));
UPDATE web_session SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE revoked_at IS NULL;
