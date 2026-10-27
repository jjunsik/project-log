"""Native synthetic requests, bounded relation plans and immutable prefix compatibility."""

import copy
import itertools
import json
from dataclasses import replace

import pytest
from test_analysis_checkpoints import MemoryCheckpoints
from test_experience_gemini import ModelConnection, model
from test_experiences import OfflineModel, evidence, inputs
from test_global_consolidation import GoldenModel, scale_state

from project_log.analysis_checkpoints import (
    PREVIOUS_RELATION_INSTRUCTION,
    RELATION_FIX_COMPATIBILITY,
    RELATION_RECOVERY_COMPATIBILITY,
    REQUEST_COMPATIBILITY,
    ResponseJournal,
    compatibility_policy,
    contract_digest,
    resume_rows,
)
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_recovery import resume_block
from project_log.experience_relations import compact_sources, pair_cover, review_schema
from project_log.experiences import (
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    Discovery,
    ExperienceGraph,
)
from project_log.gemini_request import (
    SCHEMA_BYTES,
    SCHEMA_DEPTH,
    TEXT_WIRE_BYTES,
    provider_schema,
    text_wire_bytes,
    validate_plan,
)
from project_log.git import CollectionError
from project_log.policy import sha256


def decode_sources(context, support):
    observations = []
    for table_index, row_index in support["source_ids"]:
        table = context["source_tables"][table_index]
        values = [context["source_values"][cell] for cell in table["rows"][row_index]]
        observations.append(dict(zip(table["columns"], values, strict=True)))
    return observations


def test_dictionary_preserves_all_aliases_order_and_repeated_observations():
    sources = [
        {"kind": "working_entries", "filename": "한글", "number": 1, "flag": True},
        {"kind": "commits", "oid": "x", "explicit_null": None},
    ]
    before = {
        "seeds": [],
        "supports": [
            {"id": "a", "text": "unchanged", "source": sources + sources[:1]},
            {"id": "b", "text": "unchanged too", "source": list(reversed(sources))},
        ],
    }
    frozen = copy.deepcopy(before)
    after = compact_sources(before)
    assert before == frozen and sum(len(t["rows"]) for t in after["source_tables"]) == 2
    for original, encoded in zip(before["supports"], after["supports"], strict=True):
        assert decode_sources(after, encoded) == original["source"]
        assert canonical(decode_sources(after, encoded)) == canonical(original["source"])
        assert {k: v for k, v in encoded.items() if k != "source_ids"} == {
            k: v for k, v in original.items() if k != "source"
        }


@pytest.mark.parametrize("count,capacity", [(2, 2), (16, 4), (64, 16)])
def test_split_preserves_every_original_pair_without_duplicate_subset(count, capacity):
    planned = pair_cover(list(range(count)), lambda ids: len(ids) <= capacity)
    assert all(len(part) <= capacity for part in planned)
    assert len({tuple(part) for part in planned}) == len(planned)
    assert {i for part in planned for i in part} == set(range(count))
    assert {p for part in planned for p in itertools.combinations(part, 2)} == set(
        itertools.combinations(range(count), 2)
    )


def test_unrepresentable_pair_fails_without_silently_dropping_relation():
    with pytest.raises(CollectionError, match="관계") as caught:
        pair_cover(list(range(6)), lambda ids: len(ids) == 1)
    assert caught.value.code == "analysis_request_contract_limit_relation_pair"


def test_byte_split_corrects_far_false_split_and_other_false_merges_without_dropping_sources():
    state = scale_state(20)
    state["chunks"] = tuple(
        replace(
            state["chunks"][i % 12],
            id=f"unique-{i}",
            sources_json=canonical(
                [{"kind": "working_entries", "filename": f"unique-{i}-" + "한" * 1800}]
            ),
        )
        for i in range(20)
    )
    for i, seed in enumerate(state["seeds"]):
        seed.title = f"incident-{0 if i == 19 else i} / observation {i}"
        seed.evidence_ids = [state["chunks"][i].id]
    state["calls"] = 0
    graph = ExperienceGraph(GoldenModel())
    state.update(graph._consolidate(state))
    expected = {frozenset({"seed_000000", "seed_000019"})} | {
        frozenset({f"seed_{i:06d}"}) for i in range(1, 19)
    }
    assert {frozenset(g) for g in state["group_seed_ids"]} == expected
    contexts = [c for task, _, c in graph.model.calls if task == "reconcile"]
    assert 1 < len(contexts) <= 8
    assert any(
        {"seed_000000", "seed_000019"} <= {s["seed_id"] for s in c["seeds"]} for c in contexts
    )
    for context in contexts:
        ids = [s["seed_id"] for s in context["seeds"]]
        supports = [s["id"] for s in context["supports"]]
        validate_plan(
            "reconcile",
            BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["reconcile"],
            [],
            context,
            review_schema(ids, supports).model_json_schema(),
        )
        for support in context["supports"]:
            original = next(c for c in state["chunks"] if c.id == support["evidence_id"])
            assert decode_sources(context, support) == original.payload()["sources"]


@pytest.mark.parametrize("kind", ["depth", "nodes", "bytes", "enum", "unsupported", "ref", "cycle"])
def test_complex_or_unsupported_schema_is_rejected_before_constructing_sdk_transport(kind):
    schema = {"type": "string"}
    if kind == "depth":
        for _ in range(SCHEMA_DEPTH + 1):
            schema = {"type": "array", "items": schema}
    elif kind == "nodes":
        schema = {"type": "object", "properties": {str(i): {} for i in range(1100)}}
    elif kind == "bytes":
        schema["description"] = "s" * (SCHEMA_BYTES + 1)
    elif kind == "enum":
        schema["enum"] = [str(i) for i in range(513)]
    elif kind == "unsupported":
        schema["allOf"] = [{}]
    elif kind == "ref":
        schema = {"$ref": "https://example.invalid/private"}
    else:
        schema = {"$defs": {"R": {"$ref": "#/$defs/R"}}, "$ref": "#/$defs/R"}

    class Unsupported(Discovery):
        @classmethod
        def model_json_schema(cls, **kwargs):
            return schema

    adapter = model()
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), "{}", Unsupported)
    assert caught.value.code.startswith(
        ("analysis_request_schema_invalid", "analysis_request_contract_limit_")
    )
    assert ModelConnection.calls == []


def test_native_unicode_wire_limit_has_zero_grant_quota_or_http(tmp_path):
    from project_log.gemini_free_tier import FreeTierQuota

    adapter = model()
    entered = []
    adapter.before_send = entered.append
    quota = FreeTierQuota(tmp_path / "quota.json")
    adapter.quota = quota
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), canonical({"text": "한" * 24000}), Discovery)
    assert caught.value.code == "analysis_request_contract_limit_text_bytes"
    assert not entered and not quota.path.exists()
    assert len(ModelConnection.calls) == 1 and ModelConnection.calls[0].requests == []
    assert TEXT_WIRE_BYTES < 250000 - 1024


def test_provider_projection_keeps_required_ids_enum_and_strict_local_bounds():
    schema = review_schema(["seed_0", "seed_1"], ["source_id"])
    logical = schema.model_json_schema()
    frozen = copy.deepcopy(logical)
    wire = provider_schema(logical)
    assert logical == frozen
    assert wire["properties"]["assignments"]["required"] == ["seed_0", "seed_1"]
    assert wire["$defs"]["ReviewGrounding"]["properties"]["support_ids"]["items"]["enum"] == [
        "source_id"
    ]
    assert "maxItems" not in wire["$defs"]["ReviewGrounding"]["properties"]["support_ids"]
    with pytest.raises(ValueError):
        schema.model_validate(
            {
                "assignments": {"seed_0": 0, "seed_1": 0},
                "splits": [],
                "grounds": [{"label": 0, "reason": "reason", "support_ids": ["source_id"] * 65}],
            }
        )
    adapter = model()
    adapter.complete("discover", "synthetic", (), "{}", Discovery)
    body = json.loads(ModelConnection.calls[-1].requests[0].content)
    assert body["generationConfig"]["responseJsonSchema"] == provider_schema(
        Discovery.model_json_schema()
    )
    assert text_wire_bytes(body) == len(ModelConnection.calls[-1].requests[0].content)


def prefix_fixture():
    snapshot = inputs(evidence("one", "counter before"), evidence("two", "counter after"))
    new = execution_profile("synthetic")
    old = {k: v for k, v in new.items() if k != "request_contract"}
    journal = MemoryCheckpoints()
    ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)
    rows = [
        {
            "ordinal": i,
            "task": task,
            "request_digest": digest,
            "response": json.loads(response),
            "response_digest": sha256(response.encode()),
            "contract_digest": contract_digest(snapshot.digest, old),
            "origin_request_id": "original",
            "origin_call_seq": i,
            "created_at": "unchanged",
        }
        for i, (task, digest, response) in enumerate(journal.rows, 1)
    ]
    return snapshot, old, new, rows


def test_feedback_is_bounded_nontextual_and_work_identity_ignores_capture_encoding():
    from project_log.experience_relations import (
        feedback_context,
        grounding_feedback,
        relation_work_digest,
    )

    context = {
        "seeds": [{"seed_id": "seed_a", "support_ids": ["support_a"]}],
        "supports": [{"id": "support_a", "text": "fixed evidence", "source_ids": [[0, 0]]}],
        "source_tables": [{"columns": ["read_at"], "rows": [[0]]}],
        "source_values": ["first capture"],
        "source_encoding": "encoding",
    }
    frozen = copy.deepcopy(context)
    for code in [
        "candidate_relation_grounding_merge_labels_m1_x0_d0",
        "candidate_relation_grounding_split_refs_d1_e2_x3_u4",
    ]:
        actual = feedback_context(context, code)
        assert actual.pop("relation_feedback") == grounding_feedback(code)
        assert actual == frozen and context == frozen
        assert len(canonical(feedback_context(context, code)).encode()) <= len(
            canonical(feedback_context(context)).encode()
        )
    for code in [
        "candidate_relation_grounding_invalid",
        "candidate_relation_invalid_m1_x0_v0",
        "candidate_relation_grounding_merge_refs_d0_e0_x0_u0",
        "candidate_relation_grounding_merge_refs_d0_e0_x1000001_u0",
        "private-response",
        "candidate_relation_grounding_merge_reason_blank_n1",
    ]:
        assert grounding_feedback(code) is None
        with pytest.raises(CollectionError):
            feedback_context(context, code)
    with pytest.raises(CollectionError):
        feedback_context(
            feedback_context(context, "candidate_relation_grounding_merge_labels_m1_x0_d0")
        )
    original = relation_work_digest("rules", [], context, {})
    context["source_values"] = ["second capture"]
    assert relation_work_digest("rules", [], context, {}) == original
    context["supports"][0]["text"] = "different evidence"
    assert relation_work_digest("rules", [], context, {}) != original


def test_only_reviewed_relation_instruction_pair_has_prefix_compatibility():
    current = execution_profile("synthetic")
    previous = {**current, "instruction_digest": PREVIOUS_RELATION_INSTRUCTION}
    assert compatibility_policy(previous, current) == RELATION_FIX_COMPATIBILITY
    before_recovery = {k: v for k, v in previous.items() if k != "relation_recovery"}
    assert compatibility_policy(before_recovery, previous) == RELATION_RECOVERY_COMPATIBILITY
    for key, value in [
        ("instruction_digest", "unreviewed"),
        ("model", "another-model"),
        ("request_contract", {}),
        ("relation_recovery", {}),
        ("limits", {"max_calls": 999}),
    ]:
        with pytest.raises(CollectionError) as caught:
            compatibility_policy(previous, {**current, key: value})
        assert caught.value.code == "analysis_policy_changed"
    with pytest.raises(CollectionError):
        compatibility_policy(current, previous)
    altered = {**current["relation_recovery"], "max_attempts": 3}
    with pytest.raises(CollectionError):
        compatibility_policy(
            {**previous, "relation_recovery": altered}, {**current, "relation_recovery": altered}
        )


def test_only_exact_verified_discovery_consolidation_prefix_is_reused_without_fresh_io():
    snapshot, old, new, rows = prefix_fixture()
    original = copy.deepcopy(rows)
    assert compatibility_policy(old, new) == REQUEST_COMPATIBILITY
    copied = resume_rows(rows, snapshot, old, new)
    assert copied and {r["task"] for r in copied} == {"discover", "consolidate"}
    assert len(copied) < len(rows) and rows == original
    for source, copy_row in zip(rows, copied, strict=False):
        assert {k: v for k, v in source.items() if k != "contract_digest"} == {
            k: v for k, v in copy_row.items() if k != "contract_digest"
        }
    replay = OfflineModel()
    result = ExperienceGraph(replay, checkpoints=ResponseJournal(copied, lambda *args: None)).run(
        snapshot
    )
    assert result == ExperienceGraph(OfflineModel()).run(snapshot)
    assert all(task not in {"discover", "consolidate"} for task, _, _ in replay.calls)
    for code in ("provider_invalid_request", "analysis_request_contract_limit_schema_depth"):
        assert resume_block(
            None, {"consent": {"descriptor": {"profile": old}}, "error_code": code}, new
        ) == (
            None if code == "provider_invalid_request" else "analysis_resume_request_fix_required"
        )


@pytest.mark.parametrize("change", ["digest", "reference", "profile"])
def test_prefix_compatibility_rejects_request_semantics_or_model_change(change):
    snapshot, old, new, rows = prefix_fixture()
    if change == "digest":
        rows[0]["request_digest"] = "0" * 64
    elif change == "profile":
        new["model"] = "different"
    else:
        rows[0]["response"]["seeds"][0]["evidence_ids"] = ["outside"]
        rows[0]["response_digest"] = sha256(canonical(rows[0]["response"]).encode())
    with pytest.raises(CollectionError):
        resume_rows(rows, snapshot, old, new)
