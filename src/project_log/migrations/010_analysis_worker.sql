ALTER TABLE analysis_requests ADD COLUMN owner_token uuid;
ALTER TABLE analysis_requests ADD COLUMN started_at timestamptz;
ALTER TABLE analysis_requests ADD COLUMN cancel_requested_at timestamptz;
ALTER TABLE analysis_requests ADD COLUMN phase text;
ALTER TABLE analysis_requests ADD COLUMN child_identity jsonb;
ALTER TABLE analysis_requests ADD COLUMN call_seq integer NOT NULL DEFAULT 0 CHECK (call_seq>=0);
CREATE UNIQUE INDEX analysis_single_result ON analysis_requests(result_run_id)
    WHERE result_run_id IS NOT NULL;
CREATE TABLE analysis_calls (
    request_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation uuid NOT NULL,
    owner_token uuid NOT NULL,
    seq integer NOT NULL CHECK (seq>0),
    phase text NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    entered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    outcome text NOT NULL DEFAULT 'entered' CHECK (outcome IN ('entered','responded','failed')),
    finished_at timestamptz,
    error_code text,
    PRIMARY KEY(request_id,seq),
    FOREIGN KEY(project_id,request_id) REFERENCES analysis_requests(project_id,id) ON DELETE CASCADE
);
