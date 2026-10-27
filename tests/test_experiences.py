"""Synthetic evidence/model responses; these tests never contact Gemini."""

import json

import pytest

from project_log.analysis_input import canonical
from project_log.experience_input import Evidence, ExperienceInput, Media
from project_log.experiences import (
    Assessment,
    Audit,
    Citation,
    Claim,
    ConsolidationAssignments,
    Discovery,
    ExperienceGraph,
    Finding,
    Limits,
    Reconstruction,
    Seed,
    discovery_schema,
    split_evidence,
)
from project_log.git import CollectionError


def evidence(ref, text, cid="collection-before", **extra):
    return Evidence(
        ref,
        canonical(
            {
                "kind": "working_entries",
                "collection_id": cid,
                "path_b64": "Y291bnRlci5weQ==",
                **extra,
            }
        ),
        text,
    )


def inputs(*entries):
    return ExperienceInput(
        "synthetic-project",
        "2026-10-13T00:00:00Z",
        canonical([{"id": "collection-before"}, {"id": "collection-after"}]),
        tuple(entries),
    )


class OfflineModel:
    """Deterministic protocol fixture, not a claim of real model discovery quality."""

    def __init__(self):
        self.calls = []
        self.fail_audit = False

    def complete(self, task, instruction, chunks, context, schema):
        self.calls.append((task, tuple(chunks), json.loads(context)))
        assert "근거 없는 정보는 UNKNOWN" in instruction
        if task == "discover":
            seeds = [
                Seed(
                    title="빈 입력 counter 수정",
                    rationale="경계 조건을 개선한 개발 경험",
                    evidence_ids=[c.id],
                    search_terms=["counter"],
                )
                for c in chunks
            ]
            return Discovery(seeds=seeds)
        if task == "consolidate":
            return schema.model_validate(
                {"assignments": {seed["seed_id"]: 0 for seed in json.loads(context)}}
            )
        if task == "reconcile":
            data = json.loads(context)
            ids = [s["seed_id"] for s in data["seeds"]]
            supports = sorted({ref for s in data["seeds"] for ref in s["support_ids"]})
            return schema.model_validate(
                {
                    "assignments": dict.fromkeys(ids, 0),
                    "grounds": [
                        {
                            "label": 0,
                            "reason": "Synthetic same-event relation",
                            "support_ids": supports,
                        }
                    ]
                    if len(ids) > 1
                    else [],
                    "splits": [],
                }
            )
        if task == "reconstruct":
            claims = []
            for chunk in chunks:
                if chunk.text:
                    claims.append(
                        Claim(
                            section="implementation",
                            statement=chunk.text,
                            status="EVIDENCED",
                            citations=[
                                Citation(evidence_id=chunk.id, quote=chunk.text, locator=None)
                            ],
                        )
                    )
                else:
                    claims.append(
                        Claim(
                            section="verification",
                            statement="이미지의 예시 검증 결과 해석",
                            status="INFERRED",
                            citations=[
                                Citation(evidence_id=chunk.id, quote=None, locator="page 1")
                            ],
                        )
                    )
            return Reconstruction(claims=claims)
        if task == "audit":
            return Audit(
                findings=[
                    Finding(
                        claim_index=i,
                        supported=not self.fail_audit,
                        explanation="합성 근거와 일치"
                        if not self.fail_audit
                        else "인용된 본문에서 주장한 성과를 확인할 수 없음",
                    )
                    for i in range(len(json.loads(context)))
                ]
            )
        return Assessment(category="strong", rationale="경계 조건과 구현 근거가 있는 예시")


def test_real_langgraph_discovery_retrieval_multiple_times_and_unknowns():
    model = OfflineModel()
    original = inputs(
        evidence("before", "counter: empty returned -1"),
        evidence("after", "counter: now returns 0", "collection-after"),
        Evidence("excluded", "{}", reason="suspected_secret"),
    )
    result = ExperienceGraph(model, Limits(chunk_chars=30, batch_chars=400)).run(original)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.review_status == "unreviewed"
    assert candidate.evidence_ids == ["after", "before"]
    assert {c.statement for c in candidate.claims if c.status == "EVIDENCED"} == {
        "counter: empty returned -1",
        "counter: now returns 0",
    }
    unknowns = {c.section for c in candidate.claims if c.status == "UNKNOWN"}
    assert {"alternatives", "decision", "verification", "result", "learning"} <= unknowns
    assert result.coverage["collections"] == ["collection-before", "collection-after"]
    assert result.coverage["excluded"] == [{"reference": "excluded", "reason": "suspected_secret"}]
    assert len([task for task, _, _ in model.calls if task == "discover"]) == 2
    assert {task for task, _, _ in model.calls} == {
        "discover",
        "consolidate",
        "reconcile",
        "reconstruct",
        "audit",
        "assess",
    }
    assert result.input_digest == original.digest


def test_chunking_has_no_text_gap_and_deduplicates_with_all_observation_aliases():
    text = "counter " * 300
    snapshot = inputs(evidence("first", text), evidence("second", text, "collection-after"))
    chunks = split_evidence(snapshot, Limits(chunk_chars=80, batch_chars=400))
    assert all(c.references == ("first", "second") for c in chunks)
    positions = {i for c in chunks for i in range(c.offset, c.offset + len(c.text))}
    assert positions == set(range(len(text)))
    assert all(c.text == text[c.offset : c.offset + len(c.text)] for c in chunks)


def test_native_media_separate_batches_and_unverified_interpretation():
    snapshot = inputs(
        Evidence("pdf", "{}", media=(Media("result.pdf", "application/pdf", b"%PDF-fixture"),)),
        Evidence("image", "{}", media=(Media("result.png", "image/png", b"synthetic-image"),)),
    )
    model = OfflineModel()
    chunks = split_evidence(snapshot, Limits())
    assert {ref for chunk in chunks for ref in chunk.references} == {"image", "pdf"}
    result = ExperienceGraph(model).run(snapshot)
    assert result.coverage["discovery_chunks"] == 2
    assert len(model.calls) > 0


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda c: c.model_copy(update={"citations": []}), "unsupported_claim"),
        (
            lambda c: c.model_copy(
                update={
                    "citations": [Citation(evidence_id="other-project", quote="x", locator=None)]
                }
            ),
            "invalid_evidence_reference",
        ),
        (
            lambda c: c.model_copy(
                update={
                    "citations": [
                        Citation(
                            evidence_id=c.citations[0].evidence_id,
                            quote="invented result",
                            locator=None,
                        )
                    ]
                }
            ),
            "invalid_evidence_quote",
        ),
        (lambda c: c.model_copy(update={"status": "UNKNOWN"}), "invalid_unknown_claim"),
    ],
)
def test_invalid_claims_are_rejected(change, code):
    class InvalidModel(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            if task == "reconstruct":
                result = Reconstruction(claims=[change(result.claims[0])])
            return result

    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(InvalidModel()).run(inputs(evidence("one", "counter: now returns 0")))
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("assignments", "counts"),
    [
        ({"seed_0000": 0}, "m1_x0_v0"),
        ({"seed_0000": 0, "seed_0001": 0, "seed_0002": 0}, "m0_x1_v0"),
        ({"seed_0000": 0, "seed_0002": 0}, "m1_x1_v0"),
        ({"seed_0000": -1, "seed_0001": 1}, "m0_x0_v1"),
        ({"seed_0000": 0, "seed_0001": 2}, "m0_x0_v1"),
    ],
)
def test_consolidation_cannot_drop_or_misidentify_a_candidate(assignments, counts):
    class DroppingModel(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            if task == "consolidate":
                return ConsolidationAssignments(assignments=assignments)
            return super().complete(task, instruction, chunks, context, schema)

    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(DroppingModel()).run(
            inputs(evidence("one", "before"), evidence("two", "after"))
        )
    assert exc.value.code == f"candidate_coverage_invalid_{counts}_n2"


@pytest.mark.parametrize("indices", [[0], [0, 0], [0, 2]])
def test_audit_cannot_omit_duplicate_or_reference_an_unprovided_claim(indices):
    class InvalidAudit(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            if task == "audit":
                return Audit(
                    findings=[
                        Finding(claim_index=i, supported=True, explanation="Synthetic audit")
                        for i in indices
                    ]
                )
            return result

    model = InvalidAudit()
    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(model).run(inputs(evidence("one", "before"), evidence("two", "after")))
    assert exc.value.code == "claim_audit_incomplete"
    assert model.calls[-1][0] == "audit"  # No assessment or partial completion.


def test_failed_grounding_remains_unknown_and_does_not_become_confirmed():
    model = OfflineModel()
    model.fail_audit = True
    result = ExperienceGraph(model).run(inputs(evidence("one", "counter speed improved 20%")))
    assert all(c.status == "UNKNOWN" for c in result.candidates[0].claims)
    assert result.candidates[0].review_status == "unreviewed"
    assert result.candidates[0].grounding_notes


def test_budget_failure_is_not_partial_completion():
    model = OfflineModel()
    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(model, Limits(max_calls=1)).run(inputs(evidence("one", "counter")))
    assert exc.value.code == "analysis_budget_exceeded" and len(model.calls) == 1


def test_empty_evidence_keeps_exclusions_and_does_not_call_model():
    model = OfflineModel()
    snapshot = inputs(Evidence("one", "{}", reason="body_not_preserved"))
    result = ExperienceGraph(model).run(snapshot)
    assert result.candidates == [] and result.model_calls == 0
    assert result.coverage["input_entries"] == 1 and result.coverage["excluded"]


def test_related_search_retrieves_other_timepoint_even_if_discovery_did_not_seed_it():
    class SparseModel(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            if task == "discover" and chunks[0].text == "later related implementation":
                return Discovery(seeds=[])
            return super().complete(task, instruction, chunks, context, schema)

    result = ExperienceGraph(SparseModel(), Limits(chunk_chars=40, batch_chars=300)).run(
        inputs(
            evidence("one", "counter initial problem"),
            evidence("two", "later related implementation", "collection-after"),
        )
    )
    assert result.candidates[0].evidence_ids == ["one", "two"]


def test_ambient_tracing_is_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    from langsmith import Client

    def forbidden(*args, **kwargs):
        pytest.fail("External tracing attempted")

    monkeypatch.setattr(Client, "create_run", forbidden)
    result = ExperienceGraph(OfflineModel()).run(inputs(evidence("one", "counter")))
    assert result.candidates


def test_discovery_schema_is_batch_scoped_without_changing_the_domain_parser():
    chunks = split_evidence(inputs(evidence("one", "first"), evidence("two", "second")), Limits())
    original = Discovery.model_json_schema()
    for batch in (chunks, chunks[:1]):
        schema = discovery_schema(batch)
        document = schema.model_json_schema()
        ids = document["properties"]["seeds"]["items"]["properties"]["evidence_ids"]
        assert ids["items"] == {"type": "string", "enum": [c.id for c in batch]}
        assert ids["minItems"] == 1 and "uniqueItems" not in ids
        assert schema.model_json_schema() == document
        # Generation constraints must not hide domain violations behind SDK parser errors.
        parsed = schema.model_validate(
            {
                "seeds": [
                    {
                        "title": "test",
                        "rationale": "test",
                        "evidence_ids": ["outside"],
                        "search_terms": [],
                    }
                ]
            }
        )
        assert isinstance(parsed, Discovery) and parsed.seeds[0].evidence_ids == ["outside"]
    assert Discovery.model_json_schema() == original


@pytest.mark.parametrize("kind", ["out_of_batch", "duplicate", "out_of_batch_duplicate"])
def test_discovery_rejects_reference_violations_without_normalization_or_followup(kind):
    class InvalidDiscovery(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            assert task == "discover"
            refs = {
                "out_of_batch": ["synthetic-outside-id"],
                "duplicate": [chunks[0].id, chunks[0].id],
                "out_of_batch_duplicate": ["synthetic-outside-id", "synthetic-outside-id"],
            }[kind]
            result.seeds[0].evidence_ids = refs
            self.response = result
            self.original = result.model_dump()
            return result

    model = InvalidDiscovery()
    with pytest.raises(CollectionError) as error:
        ExperienceGraph(model).run(inputs(evidence("one", "synthetic candidate body")))
    assert error.value.code == f"invalid_discovery_reference_{kind}"
    assert str(error.value) == "발견 후보의 근거 참조가 유효하지 않습니다."
    assert model.response.model_dump() == model.original
    assert len(model.calls) == 1


def test_discovery_reports_both_violation_types_even_in_different_candidates():
    class InvalidDiscovery(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            result.seeds[0].evidence_ids = ["outside"]
            result.seeds[1].evidence_ids = [chunks[1].id, chunks[1].id]
            return result

    with pytest.raises(CollectionError) as error:
        ExperienceGraph(InvalidDiscovery()).run(inputs(evidence("one", "a"), evidence("two", "b")))
    assert error.value.code == "invalid_discovery_reference_out_of_batch_duplicate"


def test_later_discovery_cannot_reference_an_earlier_batch_or_return_partial_success():
    class EarlierReference(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            assert task == "discover"
            if len(self.calls) == 1:
                self.earlier_id = chunks[0].id
            else:
                result.seeds[0].evidence_ids = [self.earlier_id]
            return result

    model = EarlierReference()
    with pytest.raises(CollectionError) as error:
        ExperienceGraph(model, Limits(chunk_chars=40, batch_chars=300)).run(
            inputs(evidence("one", "first counter"), evidence("two", "second counter"))
        )
    assert error.value.code == "invalid_discovery_reference_out_of_batch"
    assert [task for task, _, _ in model.calls] == ["discover", "discover"]


def test_different_discovery_candidates_can_share_the_same_reference():
    class SharedReference(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            if task == "discover":
                result.seeds.append(
                    result.seeds[0].model_copy(update={"title": "related candidate"})
                )
            if task == "consolidate":
                assert len(json.loads(context)) == 2
            return result

    result = ExperienceGraph(SharedReference()).run(inputs(evidence("one", "counter fix")))
    assert result.candidates[0].evidence_ids == ["one"]
