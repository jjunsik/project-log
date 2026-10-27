-- Lightweight preparation descriptors; payload bytes exist only after acceptance.
CREATE TABLE analysis_runtime (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    generation uuid,
    ready boolean NOT NULL DEFAULT false,
    CHECK (NOT ready OR generation IS NOT NULL)
);
INSERT INTO analysis_runtime DEFAULT VALUES;

CREATE TABLE analysis_preparations (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    end_collection_id uuid NOT NULL,
    generation uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL CHECK (state IN ('preparing','ready','empty','failed','consumed')),
    source_fingerprint text,
    input_digest text,
    consent_digest text,
    descriptor jsonb,
    error_code text,
    UNIQUE (project_id,id),
    CHECK (state NOT IN ('ready','empty','consumed') OR
        (source_fingerprint IS NOT NULL AND input_digest IS NOT NULL AND
         consent_digest IS NOT NULL AND descriptor IS NOT NULL))
);

CREATE TABLE analysis_requests (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    preparation_id uuid NOT NULL,
    submission_digest text NOT NULL,
    generation uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'checking' CHECK
        (state IN ('checking','rejected','queued','running','cancel_requested',
                   'completed','failed','cancelled')),
    accepted_at timestamptz,
    finished_at timestamptz,
    consent jsonb,
    input_digest text,
    input_manifest jsonb,
    input_payload jsonb,
    error_code text,
    result_run_id uuid,
    FOREIGN KEY (project_id,preparation_id)
        REFERENCES analysis_preparations(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id,result_run_id)
        REFERENCES experience_runs(project_id,id) ON DELETE RESTRICT,
    UNIQUE (project_id,id),
    CHECK ((accepted_at IS NULL) = (state IN ('checking','rejected'))),
    CHECK (accepted_at IS NULL OR
        (consent IS NOT NULL AND input_digest IS NOT NULL AND
         input_manifest IS NOT NULL AND input_payload IS NOT NULL)),
    CHECK (accepted_at IS NOT NULL OR
        (consent IS NULL AND input_digest IS NULL AND
         input_manifest IS NULL AND input_payload IS NULL)),
    CHECK ((state='completed') = (result_run_id IS NOT NULL)),
    CHECK ((state IN ('rejected','completed','failed','cancelled')) = (finished_at IS NOT NULL))
);
CREATE UNIQUE INDEX analysis_one_accepted_preparation ON analysis_requests(preparation_id)
    WHERE accepted_at IS NOT NULL;
-- Admission policy A. Project ownership/protection stays independently scoped.
CREATE UNIQUE INDEX analysis_one_active_global ON analysis_requests((true))
    WHERE state IN ('queued','running','cancel_requested');
CREATE UNIQUE INDEX analysis_one_active_project ON analysis_requests(project_id)
    WHERE state IN ('queued','running','cancel_requested');
CREATE INDEX analysis_requests_project_time ON analysis_requests(project_id,created_at DESC,id DESC);
