-- Old attempts retain their original rows/usage and are not new-policy grants.
ALTER TABLE analysis_calls ADD COLUMN logical_digest text
    CHECK (logical_digest IS NULL OR logical_digest ~ '^[a-f0-9]{64}$');
CREATE INDEX analysis_relation_logical_identity ON analysis_calls(project_id,logical_digest)
    WHERE logical_digest IS NOT NULL;
