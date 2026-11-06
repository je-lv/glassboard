-- What the author had read when it posted (ERRATA E37, item B54): the member's delivered_seq and
-- cursor_seq at that moment, stamped on every event an agent or client principal appends. Earlier
-- events, and events by humans and the board, stay NULL (shown as unknown for agents).
ALTER TABLE event ADD COLUMN read_delivered INTEGER;
ALTER TABLE event ADD COLUMN read_acked INTEGER;
