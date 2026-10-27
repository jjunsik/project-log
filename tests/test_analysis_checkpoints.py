"""Failure recovery uses synthetic inputs/models; no provider access."""

import json

import pytest
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_input import canonical
from project_log.experiences import ExperienceGraph, Limits
from project_log.git import CollectionError
from project_log.policy import sha256


class MemoryCheckpoints:
    def __init__(self):
        self.rows = []

    def load(self, index, task, digest):
        if index <= len(self.rows):
            row = self.rows[index - 1]
            assert row[:2] == (task, digest)
            return json.loads(row[2])
        assert index == len(self.rows) + 1
        return None

    def save(self, index, task, digest, output):
        assert index == len(self.rows) + 1
        self.rows.append((task, digest, canonical(output.model_dump())))

    def finish(self, calls):
        assert calls == len(self.rows)


@pytest.mark.parametrize(
    "stage", ["discover", "consolidate", "reconcile", "reconstruct", "audit", "assess"]
)
def test_resume_validated_prefix_at_every_stage_matches_uninterrupted_result(stage):
    snapshot = inputs(evidence("one", "counter before"), evidence("two", "counter after"))
    limits = Limits(chunk_chars=30, batch_chars=400)
    journal = MemoryCheckpoints()

    class FailsOnce(OfflineModel):
        def complete(self, task, *args):
            if task == stage and (stage != "discover" or self.calls):
                raise CollectionError("synthetic_failure", "Synthetic failure")
            return super().complete(task, *args)

    with pytest.raises(CollectionError, match="Synthetic failure"):
        ExperienceGraph(FailsOnce(), limits, checkpoints=journal).run(snapshot)
    prefix = list(journal.rows)
    resumed = OfflineModel()
    actual = ExperienceGraph(resumed, limits, checkpoints=journal).run(snapshot)
    baseline = OfflineModel()
    expected = ExperienceGraph(baseline, limits).run(snapshot)
    assert actual == expected
    assert len(resumed.calls) + len(prefix) == len(baseline.calls)
    assert journal.rows[: len(prefix)] == prefix


@pytest.mark.parametrize(
    "stage", ["discover", "consolidate", "reconcile", "reconstruct", "audit", "assess"]
)
def test_semantically_invalid_outputs_are_not_checkpointed(stage):
    class Invalid(OfflineModel):
        def complete(self, task, *args):
            if task != stage:
                return super().complete(task, *args)
            result = super().complete(task, *args)
            if stage == "discover":
                result.seeds[0].evidence_ids = ["unprovided"]
            elif stage == "consolidate":
                result.assignments.pop("seed_0001")
            elif stage == "reconcile":
                result.assignments.pop(next(iter(result.assignments)))
            elif stage == "reconstruct":
                result.claims[0].citations[0].quote = "invented"
            elif stage == "audit":
                result.findings = []
            else:
                result.category = "invalid"
            return result

    journal = MemoryCheckpoints()
    with pytest.raises((CollectionError, ValueError)):
        ExperienceGraph(Invalid(), checkpoints=journal).run(
            inputs(evidence("one", "counter before"), evidence("two", "counter after"))
        )
    assert all(row[0] != stage for row in journal.rows)


def test_replayed_response_is_revalidated_even_with_consistent_response_hash():
    snapshot = inputs(evidence("one", "counter evidence"))
    journal = MemoryCheckpoints()
    ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)
    task, digest, raw = journal.rows[0]
    output = json.loads(raw)
    output["seeds"][0]["evidence_ids"] = ["unprovided"]
    journal.rows[0] = (task, digest, canonical(output))
    with pytest.raises(CollectionError, match="근거 참조"):
        ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)


def test_checkpoint_before_audit_is_immutable_when_audit_rejects_claim():
    snapshot = inputs(evidence("one", "counter evidence"))
    journal = MemoryCheckpoints()
    model = OfflineModel()
    model.fail_audit = True
    result = ExperienceGraph(model, checkpoints=journal).run(snapshot)
    original = sha256(canonical(journal.rows).encode())
    replay = ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)
    assert replay == result
    assert sha256(canonical(journal.rows).encode()) == original
