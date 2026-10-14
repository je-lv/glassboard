-- Local administrator password login. Password changes invalidate earlier password sessions.
CREATE TABLE admin_password (
  principal_id TEXT PRIMARY KEY REFERENCES principal(id),
  salt BLOB NOT NULL,
  password_hash BLOB NOT NULL,
  changed_at TEXT NOT NULL
);
