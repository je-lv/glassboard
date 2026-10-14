-- Job roles explain work responsibilities. They never change the enforced member role.
CREATE TABLE role_template (
  name       TEXT PRIMARY KEY,
  version    INTEGER NOT NULL CHECK (version >= 1),
  prompt     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL REFERENCES principal(id)
);
CREATE TABLE role_template_revision (
  name       TEXT NOT NULL REFERENCES role_template(name),
  version    INTEGER NOT NULL,
  prompt     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL REFERENCES principal(id),
  PRIMARY KEY (name, version)
);
CREATE TRIGGER role_template_revision_immutable_u BEFORE UPDATE ON role_template_revision
BEGIN SELECT RAISE(ABORT, 'role prompt revisions are append-only'); END;
CREATE TRIGGER role_template_revision_immutable_d BEFORE DELETE ON role_template_revision
BEGIN SELECT RAISE(ABORT, 'role prompt revisions are append-only'); END;
ALTER TABLE member ADD COLUMN job_role TEXT REFERENCES role_template(name);
ALTER TABLE member ADD COLUMN job_role_version INTEGER;

INSERT INTO role_template(name,version,prompt,updated_at,updated_by) VALUES
('lead',1,'Coordinate the team. Propose a work split, keep dependencies and handoffs visible, and ask the human for final acceptance.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000'),
('architect',1,'Define the system contract and architecture. Review proposed design changes and record your approval or concerns before dependent implementation proceeds.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000'),
('security',1,'Review threats, trust boundaries, input validation, and failure modes. Record concrete findings and verify repairs with the team.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000'),
('builder',1,'Implement assigned components, document assumptions, record progress, and hand off testable changes.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000'),
('integrator',1,'Integrate components, resolve interfaces and merge conflicts, and report the combined system state.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000'),
('verifier',1,'Test the agreed contract, report reproducible results and gaps, and review evidence before submission.',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'00000000000000000000000000');
INSERT INTO role_template_revision(name,version,prompt,updated_at,updated_by)
SELECT name,version,prompt,updated_at,updated_by FROM role_template;
