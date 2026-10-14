-- Task division: dependencies, parent/child tasks and priority (all set by humans).
--   depends_on: JSON list of task numbers in the same session; the task is "ready" when all are done.
--   parent:     task number of the parent task, or NULL.
ALTER TABLE task ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task ADD COLUMN parent INTEGER;
ALTER TABLE task ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent'));
