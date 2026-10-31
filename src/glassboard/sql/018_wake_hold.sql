-- Wakes the supervisor may not deliver (ERRATA E31, decision 0035). A supervisor that finds the
-- agent's client session open, or cannot tell, holds the wake instead of resuming it headless: the
-- reason it reported and the seq of its `wake.held` event, until a dispatch, an inspection that
-- clears all demand, or any supervisor control clears it. `wake_dispatched_at` dates the open dispatch.
ALTER TABLE member ADD COLUMN wake_hold TEXT;
ALTER TABLE member ADD COLUMN wake_hold_seq INTEGER;
ALTER TABLE member ADD COLUMN wake_dispatched_at TEXT;
