-- Additive lifecycle and user memo fields; existing observations remain untouched.
ALTER TABLE collections ADD COLUMN title text CHECK (length(title) <= 200);
ALTER TABLE collections ADD COLUMN description text CHECK (length(description) <= 4000);
ALTER TABLE collections DROP CONSTRAINT collections_state_check;
ALTER TABLE collections ADD CONSTRAINT collections_state_check CHECK
    (state IN ('capturing','queued','running','completed','partial','failed',
               'cancel_pending','cleanup_pending'));
DROP INDEX one_active_collection;
CREATE UNIQUE INDEX one_active_collection ON collections(project_id)
    WHERE state IN ('capturing','queued','running','cancel_pending','cleanup_pending');
