-- Session-scoped full-text search (item B45, ERRATA E42). One FTS5 row per event (undelivered
-- message drafts excluded), kept by an insert trigger in the same transaction as the event. The
-- event log stays append-only; this index is derived data and can be rebuilt from it.
CREATE VIRTUAL TABLE event_fts USING fts5(text, session_id UNINDEXED, seq UNINDEXED,
                                          tokenize = 'unicode61 remove_diacritics 2');
INSERT INTO event_fts(text, session_id, seq)
  SELECT trim(COALESCE(body, '') || ' ' || COALESCE(json_extract(data, '$.title'), '') || ' ' ||
              COALESCE(json_extract(data, '$.summary'), '') || ' ' || COALESCE(json_extract(data, '$.reason'), '') || ' ' ||
              COALESCE(json_extract(data, '$.feedback'), '') || ' ' || COALESCE(json_extract(data, '$.handoff'), '')),
         session_id, seq
  FROM event WHERE type != 'message.preflight';
CREATE TRIGGER event_fts_add AFTER INSERT ON event WHEN NEW.type != 'message.preflight' BEGIN
  INSERT INTO event_fts(text, session_id, seq) VALUES (
    trim(COALESCE(NEW.body, '') || ' ' || COALESCE(json_extract(NEW.data, '$.title'), '') || ' ' ||
         COALESCE(json_extract(NEW.data, '$.summary'), '') || ' ' || COALESCE(json_extract(NEW.data, '$.reason'), '') || ' ' ||
         COALESCE(json_extract(NEW.data, '$.feedback'), '') || ' ' || COALESCE(json_extract(NEW.data, '$.handoff'), '')),
    NEW.session_id, NEW.seq);
END;
