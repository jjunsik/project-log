"""Synthetic scale, schema and deterministic Seed membership; never provider data."""

import json
from collections import Counter

import pytest
from pydantic import BaseModel
from test_analysis_checkpoints import MemoryCheckpoints
from test_experience_gemini import ModelConnection, model
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_input import canonical
from project_log.experiences import (
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    Chunk,
    ExperienceGraph,
    Limits,
    Seed,
    State,
    consolidation_schema,
    discovery_schema,
)
from project_log.git import CollectionError
from project_log.policy import sha256


def state(seeds):
    return State(chunks=(), batches=[], cursor=0, calls=0, seeds=seeds, groups=[], candidates=[])


def sized_seed(index, size):
    seed = Seed(title=f"S{index}", rationale="x", evidence_ids=["synthetic"], search_terms=[])
    seed.rationale += "x" * (size - len(seed.model_dump_json()))
    assert len(seed.model_dump_json()) == size
    return seed


def scale_seeds():
    # Only aggregate sizes from the read-only investigation, no original Seed text.
    parts = [(139, 47930), (138, 47979), (132, 47871), (134, 47762), (129, 47666), (94, 32558)]
    costs = []
    for count, total in parts:
        size, extra = divmod(total, count)
        costs.extend([size + (i < extra) for i in range(count)])
    delta = 744 - costs[1]
    costs[1] = 744
    for i in range(2, len(costs)):
        reduction = min(delta, costs[i] - 150)
        costs[i] -= reduction
        delta -= reduction
        if not delta:
            break
    return [sized_seed(i, size) for i, size in enumerate(costs)]


@pytest.mark.parametrize(
    ("count", "legacy_digest"),
    [
        (1, "3bc4ac67f600b780bab810679002eea89e25138027f6335eb177e65ff1ecb36f"),
        (2, "a5f8d4b9e4c991bc6305ef67df5bb8c8aa33c27603de0b7aeb93e195020c0cc4"),
    ],
)
def test_discovery_request_contract_remains_identical_to_pre_fix_source(count, legacy_digest):
    # Captured from the pre-fix Graph v5 Source, with synthetic chunks only.
    chunks = tuple(
        Chunk(f"synthetic-{i}", (f"ref-{i}",), "[]", "counter fix") for i in range(count)
    )
    digest = sha256(
        canonical(
            {
                "task": "discover",
                "instruction": BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["discover"],
                "chunks": [c.payload() for c in chunks],
                "context": {},
                "schema": discovery_schema(chunks).model_json_schema(),
            }
        ).encode()
    )
    assert digest == legacy_digest


def test_766_seeds_preserve_exact_membership_and_character_partition_boundaries():
    class Relations(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            # Unsorted/non-contiguous labels and reversed response key order are valid.
            result.assignments = {
                key: 7 if i % 3 else 11 for i, key in reversed(list(enumerate(result.assignments)))
            }
            return result

    seeds = scale_seeds()
    assert len(seeds) == 766
    assert sum(len(s.model_dump_json()) for s in seeds) == 271766
    assert max(len(s.model_dump_json()) for s in seeds) == 744
    fixture = Relations()
    output = ExperienceGraph(fixture)._initial_consolidate(state(seeds))
    assert [len(context) for _, _, context in fixture.calls] == [139, 138, 132, 134, 129, 94]
    assert output["calls"] == 6
    flattened = [s.title for group in output["groups"] for s in group]
    assert Counter(flattened) == Counter(s.title for s in seeds)
    assert len(flattened) == 766
    assert len(output["groups"]) == 12
    assert [s.title for s in output["groups"][0]] == [s.title for s in seeds[:139:3]]


@pytest.mark.parametrize(
    ("count", "sizes"), [(0, []), (1, []), (256, [256]), (257, [256]), (512, [256, 256])]
)
def test_count_cap_singleton_and_identical_content_are_not_deduplication(count, sizes):
    seed = sized_seed(0, 150)
    fixture = OfflineModel()
    output = ExperienceGraph(fixture)._initial_consolidate(state([seed] * count))
    assert [len(context) for _, _, context in fixture.calls] == sizes
    assert sum(len(g) for g in output["groups"]) == count
    assert all(s is seed for g in output["groups"] for s in g)


@pytest.mark.parametrize("label", [True, "0", 0.5, None])
def test_invalid_label_types_fail_without_saving_or_retrying(label):
    class RawAssignments(BaseModel):
        assignments: dict[str, object]

    class Invalid(OfflineModel):
        def complete(self, *args):
            result = super().complete(*args)
            result.assignments["seed_0000"] = label
            return RawAssignments(assignments=result.assignments)

    fixture, checkpoints = Invalid(), MemoryCheckpoints()
    with pytest.raises(CollectionError) as caught:
        ExperienceGraph(fixture, checkpoints=checkpoints)._initial_consolidate(
            state([sized_seed(0, 200), sized_seed(1, 200)])
        )
    assert caught.value.code == "candidate_coverage_invalid_schema_n2"
    assert len(fixture.calls) == 1
    assert checkpoints.rows == []


def test_one_failed_batch_does_not_save_or_advance_and_resume_reuses_validated_partition():
    class BadSecond(OfflineModel):
        def complete(self, *args):
            result = super().complete(*args)
            if len(self.calls) == 2:
                result.assignments.pop("seed_0000")
            return result

    seeds = scale_seeds()
    checkpoints = MemoryCheckpoints()
    failed = BadSecond()
    with pytest.raises(CollectionError) as caught:
        ExperienceGraph(failed, checkpoints=checkpoints)._initial_consolidate(state(seeds))
    assert caught.value.code == "candidate_coverage_invalid_m1_x0_v0_n138"
    assert len(failed.calls) == 2
    assert len(checkpoints.rows) == 1
    first = list(checkpoints.rows)
    resumed = OfflineModel()
    output = ExperienceGraph(resumed, checkpoints=checkpoints)._initial_consolidate(state(seeds))
    assert len(resumed.calls) == 5 and output["calls"] == 6
    assert checkpoints.rows[:1] == first
    baseline = ExperienceGraph(OfflineModel())._initial_consolidate(state(seeds))
    assert output == baseline


def test_replayed_consolidation_is_revalidated_without_fallback_call():
    seeds = [sized_seed(0, 200), sized_seed(1, 200)]
    checkpoints = MemoryCheckpoints()
    ExperienceGraph(OfflineModel(), checkpoints=checkpoints)._initial_consolidate(state(seeds))
    task, digest, _ = checkpoints.rows[0]
    checkpoints.rows[0] = (task, digest, canonical({"assignments": {"seed_0000": 0}}))
    fixture = OfflineModel()
    with pytest.raises(CollectionError, match="필수 Seed"):
        ExperienceGraph(fixture, checkpoints=checkpoints)._initial_consolidate(state(seeds))
    assert not fixture.calls


def test_per_execution_budget_charges_only_new_partition_calls():
    checkpoints = MemoryCheckpoints()
    seeds = scale_seeds()
    fixture = OfflineModel()
    with pytest.raises(CollectionError) as caught:
        ExperienceGraph(fixture, Limits(max_calls=5), checkpoints=checkpoints)._initial_consolidate(
            state(seeds)
        )
    assert caught.value.code == "analysis_budget_exceeded"
    assert len(checkpoints.rows) == len(fixture.calls) == 5
    prefix = list(checkpoints.rows)
    replay = OfflineModel()
    # The approved multi-day policy caps fresh attempts per execution. Replayed
    # successes cannot consume the new execution's one remaining call.
    graph = ExperienceGraph(replay, Limits(max_calls=1), checkpoints=checkpoints)
    output = graph._initial_consolidate(state(seeds))
    assert len(replay.calls) == graph.fresh_calls == 1
    assert output["calls"] == len(checkpoints.rows) == 6
    assert checkpoints.rows[:5] == prefix
    assert output == ExperienceGraph(OfflineModel())._initial_consolidate(state(seeds))
    with pytest.raises(CollectionError) as caught:
        graph._require_budget(1)
    assert caught.value.code == "analysis_budget_exceeded"
    assert len(replay.calls) == 1  # A second fresh attempt remains blocked.


@pytest.mark.parametrize("count", [2, 139, 256])
def test_native_sdk_wire_has_explicit_required_seed_keys_and_bounded_labels(count):
    context = [
        {"seed_id": f"seed_{i:04d}", **sized_seed(i, 200).model_dump()} for i in range(count)
    ]
    adapter = model("gemini-3.5-flash-lite")
    schema = consolidation_schema(count)
    output = adapter.complete(
        "consolidate",
        BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["consolidate"],
        (),
        canonical(context),
        schema,
    )
    assert set(output.assignments) == {s["seed_id"] for s in context}
    wire = json.loads(ModelConnection.calls[-1].requests[-1].content)
    assignments = wire["generationConfig"]["responseJsonSchema"]["properties"]["assignments"]
    assert assignments["required"] == list(output.assignments)
    assert assignments["additionalProperties"] is False
    assert all(
        v == {"type": "integer", "minimum": 0, "maximum": count - 1}
        for v in assignments["properties"].values()
    )
    assert len(ModelConnection.calls) == 1


def test_separate_groups_keep_grounding_and_automatic_all_stage_completion():
    class Separate(OfflineModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "consolidate":
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
            if args[0] == "reconcile":
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
                result.grounds = []
            return result

    snapshot = inputs(evidence("one", "first fix"), evidence("two", "second fix"))
    fixture, checkpoints = Separate(), MemoryCheckpoints()
    result = ExperienceGraph(fixture, checkpoints=checkpoints).run(snapshot)
    assert len(result.candidates) == 2
    assert {c.title for c in result.candidates} == {"빈 입력 counter 수정"}
    assert len({c.id for c in result.candidates}) == 2
    assert {phase for phase, _, _ in fixture.calls} == {
        "discover",
        "consolidate",
        "reconcile",
        "reconstruct",
        "audit",
        "assess",
    }
    for candidate in result.candidates:
        assert candidate.review_status == "unreviewed"
        assert candidate.evidence_ids == ["one", "two"]  # Existing related-path retrieval.
        assert all(c.citations or c.status == "UNKNOWN" for c in candidate.claims)
    replay = Separate()
    assert ExperienceGraph(replay, checkpoints=checkpoints).run(snapshot) == result
    assert replay.calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"assignments": {"seed_0000": 0}}, "m1_x0_v0_n2"),
        ({"assignments": {"seed_0000": 0, "private-invalid-id": 0}}, "m1_x1_v0_n2"),
        ({"assignments": {"seed_0000": -1, "seed_0001": 2}}, "m0_x0_v2_n2"),
        # Legacy exhaustive index arrays are not accepted as a new-contract response.
        ({"groups": [{"seed_ids": [0, 0, 1]}]}, None),
    ],
)
def test_sdk_success_can_still_fail_semantic_validation_once_without_checkpoint(response, code):
    adapter = model("gemini-3.5-flash-lite")
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.response_override = response
    adapter.test_transport_factory = lambda: connection
    checkpoints = MemoryCheckpoints()
    with pytest.raises(CollectionError) as caught:
        ExperienceGraph(adapter, checkpoints=checkpoints)._initial_consolidate(
            state([sized_seed(0, 200), sized_seed(1, 200)])
        )
    if code:
        assert caught.value.code == "candidate_coverage_invalid_" + code
    else:
        assert caught.value.code.startswith("provider_")
        assert caught.value.code.endswith("_h200")
    assert len(caught.value.code) <= 80
    assert "private-invalid-id" not in str(caught.value)
    assert len(connection.requests) == 1
    assert checkpoints.rows == []
