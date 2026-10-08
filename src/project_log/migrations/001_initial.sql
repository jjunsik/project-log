CREATE TABLE projects (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    path text NOT NULL UNIQUE,
    repository_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('new', 'ongoing', 'completed')),
    coding_agent text NOT NULL CHECK (coding_agent IN ('codex', 'none')),
    repository_info jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE collections (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    retry_of uuid REFERENCES collections(id),
    state text NOT NULL CHECK (state IN ('capturing','queued','running','completed','partial','failed')),
    snapshot jsonb NOT NULL DEFAULT '{}',
    policy jsonb NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE UNIQUE INDEX one_active_collection ON collections(project_id)
    WHERE state IN ('capturing','queued','running');
CREATE INDEX collection_queue ON collections(created_at) WHERE state = 'queued';
CREATE TABLE collection_issues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id uuid NOT NULL REFERENCES collections(id),
    phase text NOT NULL,
    code text NOT NULL,
    message text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}',
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE working_entries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id uuid NOT NULL REFERENCES collections(id),
    layer text NOT NULL,
    metadata jsonb NOT NULL,
    body bytea,
    sha256 text,
    body_reason text,
    observed_at timestamptz NOT NULL
);
CREATE INDEX working_by_collection ON working_entries(collection_id);
CREATE TABLE commits (
    collection_id uuid NOT NULL REFERENCES collections(id),
    oid text NOT NULL,
    metadata jsonb NOT NULL,
    raw bytea,
    body_reason text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, oid)
);
CREATE TABLE changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id uuid NOT NULL,
    commit_oid text NOT NULL,
    parent_oid text,
    metadata jsonb NOT NULL,
    diff bytea,
    body_reason text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (collection_id, commit_oid) REFERENCES commits(collection_id, oid)
);
CREATE INDEX changes_by_commit ON changes(collection_id, commit_oid);
CREATE TABLE head_files (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id uuid NOT NULL REFERENCES collections(id),
    metadata jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX files_by_collection ON head_files(collection_id);
