-- Distinguish ordinary supporting evidence from final files placed in the delivery area.
-- Both remain immutable artifacts with the same authorization, limits and backup behavior.
ALTER TABLE artifact ADD COLUMN purpose TEXT NOT NULL DEFAULT 'evidence'
  CHECK (purpose IN ('evidence', 'deliverable'));
