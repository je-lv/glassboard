-- Quiet hours (ERRATA E38, item B56): a daily window, in a named time zone, in which the board
-- dispatches no wake to this member (agents) or sends no browser notification (humans). Nothing is
-- hidden or dropped. quiet_urgent lets wakes caused by humans through (messages, decisions).
ALTER TABLE member ADD COLUMN quiet_start TEXT;
ALTER TABLE member ADD COLUMN quiet_end TEXT;
ALTER TABLE member ADD COLUMN quiet_tz TEXT;
ALTER TABLE member ADD COLUMN quiet_urgent INTEGER NOT NULL DEFAULT 0;
