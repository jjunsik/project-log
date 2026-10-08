-- Audit the narrowly authorized one-shot Acceptance policy reobservation.
CREATE TABLE collection_repairs (
    collection_id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    evidence_sha256 text NOT NULL,
    original_summary jsonb NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (collection_id,project_id) REFERENCES collections(id,project_id)
);
