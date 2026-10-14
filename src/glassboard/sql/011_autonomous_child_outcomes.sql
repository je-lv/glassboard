-- A human chooses this immutable authority boundary when creating a session.
ALTER TABLE session ADD COLUMN autonomous INTEGER NOT NULL DEFAULT 0 CHECK (autonomous IN (0,1));
