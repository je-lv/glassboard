-- 003: audit rows are the receipts of instance-level operations (instance-level operations store
-- their receipt on the audit row), keyed UNIQUE(actor_id, op_id).
-- Comparing a retried request with the stored one needs its digest, which the original audit table
-- lacked. event already has req_digest.
ALTER TABLE audit ADD COLUMN req_digest BLOB;
