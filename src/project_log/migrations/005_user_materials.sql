CREATE TABLE user_materials (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    filename_key text NOT NULL CHECK (length(filename_key) > 0),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    extension text NOT NULL,
    media_type text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    added_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, filename_key),
    UNIQUE (project_id, sha256)
);
CREATE INDEX user_materials_project_added ON user_materials(project_id, added_at DESC, id DESC);
