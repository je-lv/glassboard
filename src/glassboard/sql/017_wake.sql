-- Waking idle agents (ERRATA E30, decision 0035). Per member: the dispatch fence of the one
-- human-armed wake supervisor (separate from the inbox consumer), its state, the highest wake
-- generation the agent recorded as inspected, and the generation of the one outstanding dispatch.
-- A wake generation is the seq of the newest eligible event; it is derived, never stored per event.
ALTER TABLE member ADD COLUMN wake_gen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE member ADD COLUMN wake_secret TEXT;                 -- NULL = no supervisor armed
ALTER TABLE member ADD COLUMN wake_runtime TEXT;
ALTER TABLE member ADD COLUMN wake_since TEXT;
ALTER TABLE member ADD COLUMN wake_state TEXT CHECK (wake_state IN ('armed', 'paused'));
ALTER TABLE member ADD COLUMN wake_adapter TEXT;
ALTER TABLE member ADD COLUMN wake_inspected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE member ADD COLUMN wake_outstanding INTEGER;         -- generation of the open dispatch
