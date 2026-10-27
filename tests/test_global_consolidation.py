"""Known synthetic event partitions, not claims about real Gemini quality."""

import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace

import pytest
from test_analysis_checkpoints import MemoryCheckpoints
from test_consolidation import scale_seeds
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_input import canonical
from project_log.experience_relations import (
    RELATION_CONTEXT_CHARS,
    Grounding,
    Reconciliation,
    partition,
    review_schema,
    validate_review,
)
from project_log.experiences import (
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    Chunk,
    ExperienceGraph,
    Seed,
    State,
)
from project_log.gemini_request import provider_schema
from project_log.git import CollectionError


def split_grounds(context, assignments):
    previous = defaultdict(list)
    for seed in context["seeds"]:
        previous[seed["initial_group"]].append(seed)
    return [
        {
            "label": label,
            "reason": "Distinct synthetic incidents",
            "support_ids": sorted({ref for seed in members for ref in seed["support_ids"]}),
        }
        for label, members in previous.items()
        if len({assignments[s["seed_id"]] for s in members}) > 1
    ]


def golden_response(context, schema, *, event_of=None):
    labels = {}
    assignments = {}
    grouped = defaultdict(list)
    for seed in context["seeds"]:
        event = event_of(seed) if event_of else seed["title"].split(" / ")[0]
        label = labels.setdefault(event, len(labels))
        assignments[seed["seed_id"]] = label
        grouped[label].append(seed)
    return schema.model_validate(
        {
            "assignments": assignments,
            "splits": split_grounds(context, assignments),
            "grounds": [
                {
                    "label": label,
                    "reason": "Synthetic evidence identifies the same incident",
                    "support_ids": sorted({ref for seed in members for ref in seed["support_ids"]}),
                }
                for label, members in grouped.items()
                if len(members) > 1
            ],
        }
    )


class GoldenModel(OfflineModel):
    """Event labels are predeclared in the synthetic narrative before execution."""

    def complete(self, task, instruction, chunks, context, schema):
        if task == "reconcile":
            data = json.loads(context)
            self.calls.append((task, tuple(chunks), data))
            return golden_response(data, schema)
        result = super().complete(task, instruction, chunks, context, schema)
        if task == "consolidate":
            # Deliberately wrong: all unrelated incidents merged in each initial batch.
            result.assignments = dict.fromkeys(result.assignments, 0)
        return result


def scale_state(count=766):
    # Shared file/technology, twelve distinct incidents with separated occurrences.
    chunks = tuple(
        Chunk(
            f"chunk-{i}",
            (f"ref-{i}",),
            '[{"path_b64":"c2hhcmVkLnB5"}]',
            f"incident-{i}: concrete synthetic change",
        )
        for i in range(12)
    )
    seeds = []
    costs = [len(seed.model_dump_json()) for seed in scale_seeds()]
    for i in range(count):
        event = i % 12
        seed = Seed(
            title=f"incident-{event} / observation {i}",
            rationale=f"incident-{event}: independent event rationale; " + "x" * 210,
            evidence_ids=[chunks[event].id],
            search_terms=[f"incident-{event}"],
        )
        length = costs[i] - len(seed.model_dump_json()) + len(seed.rationale)
        seed.rationale = seed.rationale[:length].ljust(length, "x")
        assert len(seed.model_dump_json()) == costs[i]
        seeds.append(seed)
    return State(
        chunks=chunks,
        batches=[],
        cursor=264,
        calls=264,
        seeds=seeds,
        groups=[],
        candidates=[],
        seed_origins=[1 + i * 264 // count for i in range(count)],
        group_seed_ids=[],
        consolidation={},
        input_digest="synthetic-fixed-input",
    )


def resumed_graph(model, remaining=246):
    journal = MemoryCheckpoints()
    journal.rows = [("discover", f"old-{i}", "{}") for i in range(264)]
    return ExperienceGraph(
        model, checkpoints=journal, remaining_attempts=lambda: remaining - len(model.calls)
    ), journal


def test_766_seeds_cross_original_batches_correct_false_merges_and_finish_all_stages():
    state = scale_state()
    model = GoldenModel()
    graph, journal = resumed_graph(model)
    state.update(graph._consolidate(state))
    expected = {
        frozenset(f"seed_{i:06d}" for i in range(766) if i % 12 == event) for event in range(12)
    }
    assert {frozenset(g) for g in state["group_seed_ids"]} == expected
    trace = state["consolidation"]
    assert len(trace["initial_groups"]) > 1
    assert [len(group) for group in trace["initial_groups"]] == [139, 138, 132, 134, 129, 94]
    assert any(review["splits"] for review in trace["reviews"])
    # Actual correction: initial batches contain different incidents; final groups do not.
    assert any(len({int(key[5:]) % 12 for key in group}) > 1 for group in trace["initial_groups"])
    assert any(len({int(key[5:]) // 100 for key in group}) > 1 for group in trace["final_groups"])
    reviews = [context for task, _, context in model.calls if task == "reconcile"]
    assert len(reviews) == 24
    assert max(len(context["seeds"]) for context in reviews) <= 64
    assert {s["seed_id"] for c in reviews for s in c["seeds"]} == {
        f"seed_{i:06d}" for i in range(766)
    }
    while state["groups"]:
        state.update(graph._reconstruct(state))
    assert len(state["candidates"]) == 12
    assert len({c.id for c in state["candidates"]}) == 12
    assert {frozenset(c.seed_ids) for c in state["candidates"]} == expected
    assert {task for task, _, _ in model.calls} == {
        "consolidate",
        "reconcile",
        "reconstruct",
        "audit",
        "assess",
    }
    assert len(model.calls) < 246
    assert len(journal.rows) == state["calls"]
    assert trace["budget"]["downstream_normal_calls"] == 36
    assert len(model.calls) == 66  # six initial + 24 global reviews + 36 downstream
    assert 2 * len(model.calls) <= 246  # even one explicit 503 retry per planned call
    assert all(c.review_status == "unreviewed" for c in state["candidates"])


def test_initial_false_splits_inside_same_batch_are_corrected_by_global_review():
    class SplitEverything(GoldenModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "consolidate":
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
            return result

    state = scale_state(24)
    model = SplitEverything()
    graph, _ = resumed_graph(model)
    output = graph._consolidate(state)
    assert len(output["consolidation"]["initial_groups"]) == 24
    assert len(output["groups"]) == 12
    assert all(len(group) == 2 for group in output["groups"])


def test_identical_seed_content_in_distinct_final_groups_has_distinct_candidate_ids():
    class Separate(OfflineModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "discover":
                result.seeds = [result.seeds[0], result.seeds[0].model_copy(deep=True)]
            if args[0] == "reconcile":
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
                result.grounds = []
                result.splits = [
                    Grounding.model_validate(g)
                    for g in split_grounds(json.loads(args[3]), result.assignments)
                ]
            return result

    result = ExperienceGraph(Separate()).run(inputs(evidence("ref", "counter boundary fix")))
    assert len(result.candidates) == 2
    assert result.candidates[0].id != result.candidates[1].id
    assert result.candidates[0].seed_ids != result.candidates[1].seed_ids


@pytest.mark.parametrize(
    "kind", ["missing", "outside", "grounds", "split_grounds", "unsupported", "conflict"]
)
def test_bad_review_rejected_before_checkpoint_and_no_automatic_retry(kind):
    class Invalid(GoldenModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "reconcile":
                if kind == "missing":
                    result.assignments.pop(next(iter(result.assignments)))
                elif kind == "outside":
                    result.assignments["private-invalid-seed"] = 0
                elif kind == "grounds":
                    result.grounds = []
                elif kind == "split_grounds":
                    result.splits = []
                elif kind == "unsupported":
                    result.grounds[0].support_ids = ["private-invalid-support"]
                elif kind == "conflict" and sum(c[0] == "reconcile" for c in self.calls) == 2:
                    # Second review separates previously co-assigned overlapping Seeds.
                    result.assignments = {key: i for i, key in enumerate(result.assignments)}
                    result.grounds = []
            return result

    state, model = scale_state(80), Invalid()
    graph, journal = resumed_graph(model)
    with pytest.raises(CollectionError) as caught:
        graph._consolidate(state)
    assert caught.value.code.startswith("candidate_relation_")
    if kind == "conflict":
        assert caught.value.code == "candidate_relation_conflict"
    assert "private-invalid" not in str(caught.value)
    assert len(caught.value.code) <= 80
    review_count = sum(c[0] == "reconcile" for c in model.calls)
    assert review_count == (2 if kind == "conflict" else 1)
    assert sum(row[0] == "reconcile" for row in journal.rows) == review_count - 1


@pytest.mark.parametrize("kind", ["merge", "split"])
@pytest.mark.parametrize(
    "violation,suffix",
    [
        ("missing", "labels_m1_x0_d0"),
        ("outside_label", "labels_m1_x1_d0"),
        ("extra_label", "labels_m0_x1_d0"),
        ("duplicate_label", "labels_m0_x0_d1"),
        ("duplicate_ref", "refs_d1_e0_x0_u0"),
        ("excess_refs", "refs_d0_e1_x0_u0"),
        ("outside_ref", "refs_d0_e0_x1_u1"),
        ("uncovered", "refs_d0_e0_x0_u1"),
        ("multiple", "refs_d1_e1_x1_u1"),
    ],
)
def test_grounding_diagnostics_identify_strict_condition_without_private_values(
    kind, violation, suffix
):
    context = {
        "seeds": [
            {"seed_id": "private-seed-a", "initial_group": 97, "support_ids": ["a", "c"]},
            {
                "seed_id": "private-seed-b",
                "initial_group": 97 if kind == "split" else 98,
                "support_ids": ["b"],
            },
        ],
        "supports": [{"id": ref} for ref in ("a", "b", "c")],
    }
    ground = Grounding(
        label=97 if kind == "split" else 0, reason="private-reason", support_ids=["a", "b"]
    )
    grounds = [ground]
    if violation == "missing":
        grounds = []
    elif violation == "outside_label":
        ground.label = 999
    elif violation == "extra_label":
        grounds.append(ground.model_copy(update={"label": 999}))
    elif violation == "duplicate_label":
        grounds.append(ground.model_copy())
    else:
        ground.support_ids = {
            "duplicate_ref": ["a", "a", "b"],
            "excess_refs": ["a", "b", "c"],
            "outside_ref": ["a", "private-outside-ref"],
            "uncovered": ["a"],
            "multiple": ["a", "a", "c", "private-outside-ref"],
        }[violation]
    output = Reconciliation(
        assignments={"private-seed-a": 0, "private-seed-b": 1 if kind == "split" else 0},
        grounds=grounds if kind == "merge" else [],
        splits=grounds if kind == "split" else [],
    )
    with pytest.raises(CollectionError) as caught:
        validate_review(output, context)
    assert caught.value.code == f"candidate_relation_grounding_{kind}_{suffix}"
    assert len(caught.value.code) <= 80
    assert "private" not in caught.value.code + str(caught.value)


@pytest.mark.parametrize("kind", ["merge", "split"])
@pytest.mark.parametrize("count", [2, 32, 64])
@pytest.mark.parametrize("shared", [False, True])
def test_grounding_retains_valid_shared_or_per_member_representatives(kind, count, shared):
    context = {
        "seeds": [
            {
                "seed_id": f"seed_{i}",
                "initial_group": 97 if kind == "split" else i,
                "support_ids": ["shared"] if shared else [f"support_{i}"],
            }
            for i in range(count)
        ],
        "supports": [{"id": "shared"}]
        if shared
        else [{"id": f"support_{i}"} for i in range(count)],
    }
    ground = Grounding(
        label=97 if kind == "split" else 0,
        reason="Synthetic relationship",
        support_ids=[s["id"] for s in context["supports"]],
    )
    validate_review(
        Reconciliation(
            assignments={f"seed_{i}": i if kind == "split" else 0 for i in range(count)},
            grounds=[ground] if kind == "merge" else [],
            splits=[ground] if kind == "split" else [],
        ),
        context,
    )


@pytest.mark.parametrize("kind", ["merge", "split"])
@pytest.mark.parametrize("violation", ["unrelated_supplied_ref", "blank_reason"])
def test_grounding_rejects_unrelated_supplied_evidence_and_blank_explanations(kind, violation):
    context = {
        "seeds": [
            {"seed_id": "a", "initial_group": 97, "support_ids": ["shared"]},
            {
                "seed_id": "b",
                "initial_group": 97 if kind == "split" else 98,
                "support_ids": ["shared"],
            },
            {"seed_id": "c", "initial_group": 99, "support_ids": ["unrelated"]},
        ],
        "supports": [{"id": "shared"}, {"id": "unrelated"}],
    }
    ground = Grounding(
        label=97 if kind == "split" else 0,
        reason=" \t\n" if violation == "blank_reason" else "Synthetic relationship",
        support_ids=["shared", "unrelated"]
        if violation == "unrelated_supplied_ref"
        else ["shared"],
    )
    response = Reconciliation(
        assignments={"a": 0, "b": 1 if kind == "split" else 0, "c": 2},
        grounds=[] if kind == "split" else [ground],
        splits=[ground] if kind == "split" else [],
    )
    with pytest.raises(CollectionError) as caught:
        validate_review(response, context)
    suffix = "refs_d0_e0_x1_u0" if violation == "unrelated_supplied_ref" else "reason_blank_n1"
    assert caught.value.code == f"candidate_relation_grounding_{kind}_{suffix}"
    assert "unrelated" not in caught.value.code + str(caught.value)


@pytest.mark.parametrize("kind,count", [("merge", 24), ("split", 4)])
def test_singleton_and_unsplit_extras_keep_the_observed_error_class_strict(kind, count):
    # Synthetic same-count counterexamples, not a reconstruction of either real response.
    context = {
        "seeds": [
            {"seed_id": str(i), "initial_group": 97 + i, "support_ids": ["shared"]}
            for i in range(32)
        ],
        "supports": [{"id": "shared"}],
    }
    grounds = [
        Grounding(
            label=i if kind == "merge" else 97 + i,
            reason="Synthetic relationship",
            support_ids=["shared"],
        )
        for i in range(count)
    ]
    response = Reconciliation(
        assignments={str(i): i for i in range(32)},
        grounds=grounds if kind == "merge" else [],
        splits=grounds if kind == "split" else [],
    )
    with pytest.raises(CollectionError) as caught:
        validate_review(response, context)
    assert caught.value.code == f"candidate_relation_grounding_{kind}_labels_m0_x{count}_d0"
    validate_review(response.model_copy(update={"grounds": [], "splits": []}), context)


def test_grounding_reference_counts_include_all_invalid_groups_without_changing_rejection():
    context = {
        "seeds": [
            {"seed_id": str(i), "initial_group": i, "support_ids": [str(i)]} for i in range(4)
        ],
        "supports": [{"id": str(i)} for i in range(4)],
    }
    output = Reconciliation(
        assignments={"0": 0, "1": 0, "2": 1, "3": 1},
        grounds=[
            Grounding(label=0, reason="private", support_ids=["0", "0"]),
            Grounding(label=1, reason="private", support_ids=["2", "private-outside"]),
        ],
        splits=[],
    )
    with pytest.raises(CollectionError) as caught:
        validate_review(output, context)
    assert caught.value.code == "candidate_relation_grounding_merge_refs_d1_e0_x1_u2"
    assert "private" not in caught.value.code + str(caught.value)


def test_global_chain_conflict_does_not_silently_overmerge():
    with pytest.raises(CollectionError, match="모순"):
        partition(
            ["a", "b", "c"],
            [
                {"assignments": {"a": 0, "b": 0}},
                {"assignments": {"b": 0, "c": 0}},
                {"assignments": {"a": 0, "c": 1}},
            ],
        )


def test_large_plan_makes_partial_progress_until_execution_budget_without_partial_success():
    model = GoldenModel()
    graph, journal = resumed_graph(model, remaining=1)
    with pytest.raises(CollectionError) as caught:
        graph._consolidate(scale_state())
    assert caught.value.code == "analysis_budget_exceeded"
    assert len(model.calls) == 1
    assert len(journal.rows) == 265


def test_full_checkpoint_replay_of_global_review_is_identical_and_free_of_model_calls():
    snapshot = inputs(evidence("one", "counter before"), evidence("two", "counter after"))
    journal = MemoryCheckpoints()
    first = ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)
    second_model = OfflineModel()
    second = ExperienceGraph(second_model, checkpoints=journal, remaining_attempts=lambda: 0).run(
        snapshot
    )
    assert first == second
    assert second_model.calls == []


def test_large_final_group_uses_bounded_focus_context_without_losing_membership():
    state = scale_state()
    graph = ExperienceGraph(OfflineModel())
    ids = [f"seed_{i:06d}" for i in range(766)]
    context = graph._reconstruction_context(state["seeds"], ids, state["chunks"])
    assert isinstance(context, dict)
    assert context["seed_ids"] == ids
    assert 0 < len(context["focus_seeds"]) < 766


def test_fully_cached_empty_reconstruction_completes_at_zero_remaining_attempts():
    class EmptyClaims(OfflineModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "reconstruct":
                result.claims = []
            return result

    snapshot = inputs(evidence("one", "counter before"), evidence("two", "counter after"))
    journal = MemoryCheckpoints()
    first = ExperienceGraph(EmptyClaims(), checkpoints=journal).run(snapshot)
    model = EmptyClaims()
    second = ExperienceGraph(model, checkpoints=journal, remaining_attempts=lambda: 0).run(snapshot)
    assert first == second
    assert model.calls == []


def test_window_planning_is_stable_across_process_hash_seeds():
    script = """import json
from project_log.experience_relations import windows
seeds=[dict(title=f"incident-{i%12}",
    rationale=f"counter incident-{i%12} decision-{i%7}",
    search_terms=["shared",f"term{i%9}"]) for i in range(100)]
print(json.dumps(windows(seeds,[{f"source{i%8}","common"} for i in range(100)],96000)))"""
    results = [
        subprocess.check_output(
            [sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": value}, text=True
        )
        for value in ("1", "17", "123")
    ]
    assert results[0] == results[1] == results[2]


def test_rare_event_query_links_distant_distinct_evidence_across_shared_file():
    state = scale_state(100)
    state["calls"] = 0
    state["chunks"] = tuple(
        Chunk(
            f"independent-{i}",
            (f"ref-{i}",),
            '[{"path_b64":"c2hhcmVkLnB5"}]',
            f"Synthetic evidence observation {i}",
        )
        for i in range(100)
    )
    state["seeds"] = [
        Seed(
            title=(
                f"{'empty-counter-regression' if i in (0, 99) else f'other-event-{i}'}"
                " / observation"
            ),
            rationale="Individual concrete incident",
            evidence_ids=[f"independent-{i}"],
            search_terms=["empty_counter_regression"] if i in (0, 99) else [f"separate_{i}"],
        )
        for i in range(100)
    ]
    model = GoldenModel()
    output = ExperienceGraph(model)._consolidate(state)
    assert {"seed_000000", "seed_000099"} in [set(g) for g in output["group_seed_ids"]]
    assert len(output["group_seed_ids"]) == 99
    first = next(context for task, _, context in model.calls if task == "reconcile")
    assert {"seed_000000", "seed_000099"} <= {s["seed_id"] for s in first["seeds"]}


def test_766_singleton_plan_is_advisory_and_keeps_every_candidate_for_later_execution():
    class Distinct(GoldenModel):
        def complete(self, *args):
            result = super().complete(*args)
            if args[0] == "reconcile":
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
                result.grounds = []
                result.splits = [
                    Grounding.model_validate(g)
                    for g in split_grounds(json.loads(args[3]), result.assignments)
                ]
            return result

    state, model = scale_state(), Distinct()
    graph, journal = resumed_graph(model)
    state.update(graph._consolidate(state))
    assert len(state["groups"]) == 766
    assert len(model.calls) == 30
    assert len(journal.rows) == 264 + 30
    assert state["consolidation"]["budget"]["plan_is_advisory"]
    assert state["consolidation"]["budget"]["downstream_normal_calls"] > 246
    state.update(graph._reconstruct(state))
    assert any(task == "reconstruct" for task, _, _ in model.calls)
    assert len(state["candidates"]) == 1 and len(state["groups"]) == 765


def test_large_repeated_source_metadata_is_encoded_losslessly_without_extra_calls():
    state = scale_state(80)
    aliases = json.dumps(
        [
            {"filename": f"observation-{i}-" + "x" * 600, "kind": "working_entries"}
            for i in range(12)
        ]
    )
    state["chunks"] = tuple(replace(chunk, sources_json=aliases) for chunk in state["chunks"])
    graph = ExperienceGraph(OfflineModel())
    graph.seed_ids = [f"seed_{i:06d}" for i in range(80)]
    contexts = graph._review_contexts(state, [list(range(80))])
    assert len(contexts) == 3  # The original retrieval windows now fit without subdivision.
    for context in contexts:
        for support in context["supports"]:
            assert [
                dict(
                    zip(
                        context["source_tables"][ti]["columns"],
                        [
                            context["source_values"][v]
                            for v in context["source_tables"][ti]["rows"][ri]
                        ],
                        strict=True,
                    )
                )
                for ti, ri in support["source_ids"]
            ] == json.loads(aliases)
    assert {s["seed_id"] for c in contexts for s in c["seeds"]} == set(graph.seed_ids)
    assert all(len(canonical(c)) + 2 <= RELATION_CONTEXT_CHARS for c in contexts)
    assert graph.model.calls == []


def test_native_sdk_wire_binds_64_global_assignments_and_supplied_grounding_ids():
    from test_experience_gemini import ModelConnection, model

    state = scale_state(64)
    graph = ExperienceGraph(OfflineModel())
    graph.seed_ids = [f"seed_{i:06d}" for i in range(64)]
    context = graph._review_contexts(state, [list(range(64))])[0]
    assert len(context["seeds"]) == 64
    schema = review_schema(
        [s["seed_id"] for s in context["seeds"]], [s["id"] for s in context["supports"]]
    )
    adapter = model("gemini-3.5-flash-lite")
    adapter.complete(
        "reconcile",
        BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["reconcile"],
        (),
        canonical(context),
        schema,
    )
    body = json.loads(ModelConnection.calls[-1].requests[-1].content)
    assert body["generationConfig"]["responseJsonSchema"] == provider_schema(
        schema.model_json_schema()
    )
    assert body["generationConfig"]["maxOutputTokens"] == 8192
    assert (
        len(body["generationConfig"]["responseJsonSchema"]["properties"]["assignments"]["required"])
        == 64
    )
    assert len(ModelConnection.calls[-1].requests) == 1
