-- 004: running size of each session's log, so the storage cap (50k events or 200 MiB per
-- session) is checked without summing the log on every write.
-- Counts the UTF-8 bytes of event body + data, which is what participants control.
ALTER TABLE session ADD COLUMN bytes_used INTEGER NOT NULL DEFAULT 0;
UPDATE session SET bytes_used = (
  SELECT COALESCE(SUM(LENGTH(CAST(COALESCE(body, '') AS BLOB)) + LENGTH(CAST(COALESCE(data, '') AS BLOB))), 0)
  FROM event WHERE event.session_id = session.id);
