-- Independent execution evidence: no historical row is rewritten or promoted.
CREATE TABLE analysis_events (
    request_id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_seq bigint NOT NULL CHECK (event_seq>0),
    kind text NOT NULL,
    phase text NOT NULL,
    call_seq_observed integer NOT NULL CHECK (call_seq_observed>=0),
    unit_digest text CHECK (unit_digest IS NULL OR unit_digest ~ '^[a-f0-9]{64}$'),
    data jsonb NOT NULL CHECK (jsonb_typeof(data)='object'),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(request_id,event_seq),
    FOREIGN KEY(project_id,request_id) REFERENCES analysis_requests(project_id,id) ON DELETE CASCADE
);
