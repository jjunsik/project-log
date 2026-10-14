-- Append-only completed analysis revisions. Execution lifecycle is added in stage 3.
CREATE TABLE experience_runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    input_digest text NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
    input_manifest jsonb NOT NULL,
    result jsonb NOT NULL,
    model text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('isolated', 'live')),
    UNIQUE (project_id,id)
);
CREATE INDEX experience_runs_project_time ON experience_runs(project_id,created_at DESC,id DESC);
CREATE TABLE experience_evidence (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    reference text NOT NULL,
    source jsonb NOT NULL,
    body text,
    media jsonb NOT NULL DEFAULT '[]',
    reason text,
    limitations jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id,reference),
    FOREIGN KEY (project_id,run_id) REFERENCES experience_runs(project_id,id) ON DELETE CASCADE
);
