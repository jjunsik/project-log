-- Keep the existing immutable response journal; allow its new consolidation review task.
-- No columns, stored responses, origins, attempts or previous execution records change.
ALTER TABLE analysis_checkpoints DROP CONSTRAINT analysis_checkpoints_task_check;
ALTER TABLE analysis_checkpoints ADD CONSTRAINT analysis_checkpoints_task_check
    CHECK (task IN ('discover','consolidate','reconcile','reconstruct','audit','assess'));
