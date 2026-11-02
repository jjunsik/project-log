-- Additive metadata only: historical calls and completed results retain their values.
ALTER TABLE analysis_calls ADD COLUMN unit_request_digest text
    CHECK (unit_request_digest IS NULL OR unit_request_digest ~ '^[a-f0-9]{64}$');
ALTER TABLE analysis_requests ADD COLUMN result_partial boolean NOT NULL DEFAULT false;
ALTER TABLE analysis_requests DROP CONSTRAINT analysis_requests_check3;
ALTER TABLE analysis_requests ADD CONSTRAINT analysis_result_state CHECK (
    (state='completed' AND result_run_id IS NOT NULL AND NOT result_partial)
    OR (state='failed' AND result_run_id IS NOT NULL AND result_partial AND error_code IS NOT NULL)
    OR (state<>'completed' AND result_run_id IS NULL AND NOT result_partial)
);
