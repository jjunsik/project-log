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
    Consolidation,
    Discovery,
    ExperienceGraph,
    Finding,
    Group,
    Limits,
    Reconstruction,
    Seed,
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
            return Consolidation(groups=[Group(seed_ids=list(range(len(json.loads(context)))))])
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
    result = ExperienceGraph(model).run(snapshot)
    assert result.coverage["discovered_references"] == ["image", "pdf"]
    assert all(len(chunks) == 1 for task, chunks, _ in model.calls if task == "discover")
    assert all(c.status != "EVIDENCED" for c in result.candidates[0].claims)
    assert result.candidates[0].category == "potential"


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


def test_consolidation_cannot_drop_a_candidate():
    class DroppingModel(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            if task == "consolidate":
                return Consolidation(groups=[Group(seed_ids=[0])])
            return super().complete(task, instruction, chunks, context, schema)

    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(DroppingModel()).run(
            inputs(evidence("one", "before"), evidence("two", "after"))
        )
    assert exc.value.code == "candidate_coverage_invalid"


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
