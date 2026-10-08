-- User configuration, separate from registration-time repository observations.
-- Legacy projects remain unset; neither projects nor collections are backfilled.
ALTER TABLE projects ADD COLUMN base_branch text;
ALTER TABLE projects ADD CONSTRAINT project_base_branch_nonblank
    CHECK (base_branch IS NULL OR length(btrim(base_branch)) > 0);
