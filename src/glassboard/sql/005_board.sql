-- Task progress and blockers for the board view.
--   checklist:  JSON list of {"id": n, "text": "...", "done": bool}; set by humans, ticked by the claimant.
--   waiting_on: JSON {"kind": "question"|"task"|"member", "ref": seq|num|principal id, "label": "#12"|"T3"|"@alice"}
--               while the task is blocked; NULL otherwise.
ALTER TABLE task ADD COLUMN checklist TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task ADD COLUMN waiting_on TEXT;
