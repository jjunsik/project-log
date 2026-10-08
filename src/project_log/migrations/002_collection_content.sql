-- Observation rows retain their identity/time; immutable data is shared within a Project.
CREATE TABLE contents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    sha256 text NOT NULL,
    body bytea NOT NULL,
    UNIQUE (project_id, sha256),
    UNIQUE (id, project_id),
    CHECK (sha256 = encode(sha256(body), 'hex'))
);
ALTER TABLE collections ADD COLUMN kind text NOT NULL DEFAULT 'initial'
    CHECK (kind IN ('initial','manual','retry'));
ALTER TABLE collections ADD COLUMN baseline_id uuid REFERENCES collections(id);
ALTER TABLE collections ADD UNIQUE (id, project_id);
UPDATE collections SET kind='retry' WHERE retry_of IS NOT NULL;

CREATE TABLE git_commits (
    project_id uuid NOT NULL REFERENCES projects(id),
    oid text NOT NULL,
    metadata jsonb NOT NULL,
    PRIMARY KEY (project_id, oid)
);
CREATE TABLE git_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id uuid NOT NULL,
    commit_oid text NOT NULL,
    parent_oid text,
    old_path_b64 text NOT NULL,
    new_path_b64 text NOT NULL,
    metadata jsonb NOT NULL,
    UNIQUE NULLS NOT DISTINCT (project_id,commit_oid,parent_oid,old_path_b64,new_path_b64),
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id,commit_oid) REFERENCES git_commits(project_id,oid)
);
CREATE TABLE git_files (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    commit_oid text NOT NULL,
    path_b64 text NOT NULL,
    metadata jsonb NOT NULL,
    UNIQUE (project_id,commit_oid,path_b64),
    UNIQUE (id, project_id)
);

ALTER TABLE working_entries ADD COLUMN project_id uuid;
ALTER TABLE working_entries ADD COLUMN content_id bigint;
ALTER TABLE working_entries ADD COLUMN body_observed_at timestamptz;
ALTER TABLE working_entries ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}';
ALTER TABLE commits ADD COLUMN project_id uuid;
ALTER TABLE commits ADD COLUMN content_id bigint;
ALTER TABLE changes ADD COLUMN project_id uuid;
ALTER TABLE changes ADD COLUMN content_id bigint;
ALTER TABLE changes ADD COLUMN git_change_id bigint;
ALTER TABLE head_files ADD COLUMN project_id uuid;
ALTER TABLE head_files ADD COLUMN git_file_id bigint;
UPDATE working_entries w SET project_id=c.project_id FROM collections c WHERE w.collection_id=c.id;
UPDATE commits m SET project_id=c.project_id FROM collections c WHERE m.collection_id=c.id;
UPDATE changes d SET project_id=c.project_id FROM collections c WHERE d.collection_id=c.id;
UPDATE head_files h SET project_id=c.project_id FROM collections c WHERE h.collection_id=c.id;

-- Populate once before switching references. No observation/content DELETE or table replacement.
INSERT INTO contents(project_id,sha256,body)
SELECT DISTINCT project_id,encode(sha256(body),'hex'),body FROM (
    SELECT project_id,body FROM working_entries WHERE body IS NOT NULL
    UNION ALL SELECT project_id,raw FROM commits WHERE raw IS NOT NULL
    UNION ALL SELECT project_id,diff FROM changes WHERE diff IS NOT NULL
) source;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM working_entries
        WHERE body IS NOT NULL AND sha256 IS DISTINCT FROM encode(sha256(body),'hex')) THEN
        RAISE EXCEPTION 'Legacy working body checksum mismatch';
    END IF;
END $$;
UPDATE working_entries w SET content_id=b.id,body_observed_at=w.observed_at,
    provenance=jsonb_build_object('kind','original_collection_capture'),body=NULL
    FROM contents b WHERE b.project_id=w.project_id AND b.sha256=encode(sha256(w.body),'hex');
UPDATE commits m SET content_id=b.id,raw=NULL FROM contents b
    WHERE b.project_id=m.project_id AND b.sha256=encode(sha256(m.raw),'hex');
UPDATE changes d SET content_id=b.id,diff=NULL FROM contents b
    WHERE b.project_id=d.project_id AND b.sha256=encode(sha256(d.diff),'hex');

INSERT INTO git_commits(project_id,oid,metadata)
    SELECT DISTINCT ON (project_id,oid) project_id,oid,metadata FROM commits
    ORDER BY project_id,oid,observed_at;
INSERT INTO git_changes(project_id,commit_oid,parent_oid,old_path_b64,new_path_b64,metadata)
    SELECT DISTINCT ON (project_id,commit_oid,parent_oid,metadata->'old'->>'path_b64',
        metadata->'new'->>'path_b64') project_id,commit_oid,parent_oid,
        metadata->'old'->>'path_b64',metadata->'new'->>'path_b64',metadata FROM changes
    ORDER BY project_id,commit_oid,parent_oid,metadata->'old'->>'path_b64',
        metadata->'new'->>'path_b64',id;
INSERT INTO git_files(project_id,commit_oid,path_b64,metadata)
    SELECT DISTINCT ON (project_id,metadata->>'commit',metadata->>'path_b64')
        project_id,metadata->>'commit',metadata->>'path_b64',metadata FROM head_files
    ORDER BY project_id,metadata->>'commit',metadata->>'path_b64',id;
ALTER TABLE commits ALTER COLUMN metadata DROP NOT NULL;
ALTER TABLE changes ALTER COLUMN metadata DROP NOT NULL;
ALTER TABLE head_files ALTER COLUMN metadata DROP NOT NULL;
UPDATE commits m SET metadata=NULL FROM git_commits g
    WHERE g.project_id=m.project_id AND g.oid=m.oid AND g.metadata=m.metadata;
UPDATE changes d SET git_change_id=g.id,
    metadata=CASE WHEN d.metadata=g.metadata THEN NULL ELSE d.metadata END FROM git_changes g
    WHERE g.project_id=d.project_id AND g.commit_oid=d.commit_oid
    AND g.parent_oid IS NOT DISTINCT FROM d.parent_oid
    AND g.old_path_b64=d.metadata->'old'->>'path_b64'
    AND g.new_path_b64=d.metadata->'new'->>'path_b64';
UPDATE head_files h SET git_file_id=g.id,
    metadata=CASE WHEN h.metadata=g.metadata THEN NULL ELSE h.metadata END FROM git_files g
    WHERE g.project_id=h.project_id AND g.commit_oid=h.metadata->>'commit'
    AND g.path_b64=h.metadata->>'path_b64';

ALTER TABLE working_entries ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE commits ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE changes ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE changes ALTER COLUMN git_change_id SET NOT NULL;
ALTER TABLE head_files ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE head_files ALTER COLUMN git_file_id SET NOT NULL;
ALTER TABLE working_entries ADD UNIQUE (id,project_id);
ALTER TABLE working_entries ADD FOREIGN KEY (collection_id,project_id) REFERENCES collections(id,project_id);
ALTER TABLE working_entries ADD FOREIGN KEY (content_id,project_id) REFERENCES contents(id,project_id);
ALTER TABLE commits ADD FOREIGN KEY (collection_id,project_id) REFERENCES collections(id,project_id);
ALTER TABLE commits ADD FOREIGN KEY (project_id,oid) REFERENCES git_commits(project_id,oid);
ALTER TABLE commits ADD FOREIGN KEY (content_id,project_id) REFERENCES contents(id,project_id);
ALTER TABLE changes ADD FOREIGN KEY (collection_id,project_id) REFERENCES collections(id,project_id);
ALTER TABLE changes ADD FOREIGN KEY (git_change_id,project_id) REFERENCES git_changes(id,project_id);
ALTER TABLE changes ADD FOREIGN KEY (content_id,project_id) REFERENCES contents(id,project_id);
ALTER TABLE head_files ADD FOREIGN KEY (collection_id,project_id) REFERENCES collections(id,project_id);
ALTER TABLE head_files ADD FOREIGN KEY (git_file_id,project_id) REFERENCES git_files(id,project_id);

CREATE TABLE working_repairs (
    working_entry_id bigint PRIMARY KEY,
    project_id uuid NOT NULL,
    content_id bigint,
    body_reason text,
    body_observed_at timestamptz NOT NULL,
    read_metadata jsonb NOT NULL,
    provenance jsonb NOT NULL,
    FOREIGN KEY (working_entry_id,project_id) REFERENCES working_entries(id,project_id),
    FOREIGN KEY (content_id,project_id) REFERENCES contents(id,project_id)
);
CREATE VIEW working_records AS SELECT w.id,w.collection_id,w.project_id,w.layer,w.metadata,
    COALESCE(b.body,w.body) AS body,
    CASE WHEN r.working_entry_id IS NOT NULL THEN b.sha256 ELSE w.sha256 END AS sha256,
    CASE WHEN r.working_entry_id IS NOT NULL THEN r.body_reason ELSE w.body_reason END AS body_reason,
    w.observed_at,COALESCE(r.body_observed_at,w.body_observed_at) AS body_observed_at,
    CASE WHEN r.working_entry_id IS NOT NULL THEN r.content_id ELSE w.content_id END AS content_id,
    CASE WHEN r.working_entry_id IS NOT NULL THEN r.provenance ELSE w.provenance END AS provenance,
    w.body_reason AS original_body_reason,r.read_metadata AS repair_read_metadata,
    r.working_entry_id IS NOT NULL AS repaired
    FROM working_entries w LEFT JOIN working_repairs r ON r.working_entry_id=w.id
    LEFT JOIN contents b ON b.id=CASE WHEN r.working_entry_id IS NOT NULL
        THEN r.content_id ELSE w.content_id END;
CREATE VIEW commit_records AS SELECT m.collection_id,m.project_id,m.oid,
    COALESCE(m.metadata,g.metadata) AS metadata,COALESCE(b.body,m.raw) AS raw,
    m.content_id,m.body_reason,m.observed_at FROM commits m
    JOIN git_commits g ON g.project_id=m.project_id AND g.oid=m.oid
    LEFT JOIN contents b ON b.id=m.content_id;
CREATE VIEW change_records AS SELECT d.id,d.collection_id,d.project_id,d.commit_oid,d.parent_oid,
    COALESCE(d.metadata,g.metadata) AS metadata,COALESCE(b.body,d.diff) AS diff,
    d.content_id,d.git_change_id,d.body_reason,d.observed_at FROM changes d
    JOIN git_changes g ON g.id=d.git_change_id LEFT JOIN contents b ON b.id=d.content_id;
CREATE VIEW head_records AS SELECT h.id,h.collection_id,h.project_id,
    COALESCE(h.metadata,g.metadata) AS metadata,h.git_file_id,h.observed_at FROM head_files h
    JOIN git_files g ON g.id=h.git_file_id;
