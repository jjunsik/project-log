-- Explicit failure recovery creates a new consented request; terminal rows stay terminal.
-- Preparation intent is metadata, checked against a project-owned request at acceptance.
ALTER TABLE analysis_preparations ADD COLUMN resume_from_request_id uuid;
ALTER TABLE analysis_requests ADD COLUMN resume_from_request_id uuid;
ALTER TABLE analysis_requests ADD COLUMN inherited_attempts integer NOT NULL DEFAULT 0
    CHECK (inherited_attempts>=0);
ALTER TABLE analysis_requests ADD FOREIGN KEY (project_id,resume_from_request_id)
    REFERENCES analysis_requests(project_id,id);
ALTER TABLE analysis_requests ADD CHECK (resume_from_request_id IS DISTINCT FROM id);
CREATE UNIQUE INDEX analysis_one_accepted_resume ON analysis_requests(resume_from_request_id)
    WHERE accepted_at IS NOT NULL AND resume_from_request_id IS NOT NULL;

CREATE TABLE analysis_checkpoints (
    request_id uuid NOT NULL,
    project_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal>0),
    task text NOT NULL CHECK (task IN ('discover','consolidate','reconstruct','audit','assess')),
    contract_digest text NOT NULL CHECK (contract_digest ~ '^[a-f0-9]{64}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[a-f0-9]{64}$'),
    response jsonb NOT NULL CHECK (jsonb_typeof(response)='object'),
    response_digest text NOT NULL CHECK (response_digest ~ '^[a-f0-9]{64}$'),
    origin_request_id uuid NOT NULL,
    origin_call_seq integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id,ordinal),
    FOREIGN KEY (project_id,request_id) REFERENCES analysis_requests(project_id,id) ON DELETE CASCADE,
    FOREIGN KEY (project_id,origin_request_id) REFERENCES analysis_requests(project_id,id),
    FOREIGN KEY (origin_request_id,origin_call_seq) REFERENCES analysis_calls(request_id,seq)
);
