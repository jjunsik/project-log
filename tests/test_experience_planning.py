"""Full wire planning with synthetic data; no provider, credentials or production DB."""

import json

import pytest
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_checkpoints import UnitResponseJournal
from project_log.experiences import (
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    Citation,
    Claim,
    ExperienceGraph,
    Limits,
    Reconstruction,
    Seed,
    batches,
    plan_discovery,
    resolve_chunks,
    split_evidence,
)
from project_log.gemini_request import TEXT_WIRE_BYTES, validate_plan
from project_log.git import CollectionError


class CheckedModel(OfflineModel):
    def complete(self, task, instruction, chunks, context, schema):
        validate_plan(
            task,
            instruction,
            [c.payload() for c in chunks],
            json.loads(context),
            schema.model_json_schema(),
        )
        if task == "reconstruct":
            self.calls.append((task, tuple(chunks), json.loads(context)))
            return Reconstruction(
                claims=[
                    Claim(
                        section="implementation",
                        statement=c.text[:100],
                        status="EVIDENCED",
                        citations=[Citation(evidence_id=c.id, quote=c.text[:100], locator=None)],
                    )
                    for c in chunks
                    if c.text
                ]
            )
        return super().complete(task, instruction, chunks, context, schema)


@pytest.mark.parametrize("body", ["ASCII " * 5000, '한글"\\\n' * 6000])
def test_discovery_and_reconstruction_slices_keep_all_text_offsets_and_aliases(body):
    source = inputs(evidence("one", body), evidence("alias", body, cid="collection-after"))
    limits = Limits(chunk_chars=48000, batch_chars=48000)
    original = split_evidence(source, limits)
    work, origins = plan_discovery(original, limits, True)
    covered = set()
    for part in work:
        for chunk in part:
            assert (
                chunk.references == ("one", "alias")
                and chunk.sources_json == original[0].sources_json
            )
            covered.update(range(chunk.offset, chunk.offset + len(chunk.text)))
    assert covered == set(range(len(body)))
    assert len(set(origins)) == len(work)
    resolved = resolve_chunks(original, [c.id for part in work for c in part])
    assert list(resolved) == [c for part in work for c in part]
    model = CheckedModel()
    result = ExperienceGraph(model, limits, isolate_failures=True, plan_requests=True).run(source)
    assert result.coverage["analysis_status"] == "complete"
    assert result.candidates and result.candidates[0].evidence_ids == ["alias", "one"]
    assert all(c.id in {v["id"] for v in result.coverage["chunks"]} for c in resolved)


def test_reconstruction_batch_is_resplit_using_full_context_without_dropping_chunks():
    source = inputs(*(evidence(str(i), "한글" * 3000 + str(i)) for i in range(4)))
    chunks = split_evidence(source, Limits())
    group = [
        Seed(
            title="태그 중복 제거",
            rationale="합성 근거",
            evidence_ids=[c.id],
            search_terms=["backend"],
        )
        for c in chunks
    ]
    graph = ExperienceGraph(CheckedModel(), plan_requests=True)
    original = batches(chunks, Limits())

    def context(part):
        return graph._reconstruction_context(group, [str(i) for i in range(4)], part)

    work = graph._chunk_work("reconstruct", original, context, lambda part: Reconstruction)
    assert len(work) > len(original)
    assert [c.id for part in work for c in part] == [c.id for c in chunks]
    assert all(context(part) == [seed.model_dump() for seed in group] for part in work)
    for part in work:
        validate_plan(
            "reconstruct",
            BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["reconstruct"],
            [c.payload() for c in part],
            context(part),
            Reconstruction.model_json_schema(),
        )


def test_unrepresentable_source_metadata_is_isolated_without_losing_independent_candidate():
    source = inputs(
        *(
            evidence(
                f"alias-{i}", "metadata only", path_b64="b3RoZXI=", filename=f"{i}-" + "한" * 1000
            )
            for i in range(40)
        ),
        evidence("good", "Independent counter evidence"),
    )
    model = CheckedModel()
    events = []
    result = ExperienceGraph(
        model, isolate_failures=True, plan_requests=True, events=lambda *event: events.append(event)
    ).run(source)
    assert result.coverage["analysis_status"] == "partial" and len(result.candidates) == 1
    rejected = [e for e in events if e[0] == "unit_failed" and e[1] == "discover"]
    assert rejected and all(e[3]["measured_bytes"] > TEXT_WIRE_BYTES for e in rejected)
    assert all(e[3]["cause_status"] == "CONFIRMED" for e in rejected)
    assert all(not any(c.text == "metadata only" for c in chunks) for _, chunks, _ in model.calls)


def test_retrieval_priority_keeps_every_original_weak_match_and_preserves_completed_requests():
    source = inputs(
        evidence("weak", "backend unrelated", path_b64="d2Vhaw==", commit_oid="same"),
        evidence("anchor", "counter tag dedup", commit_oid="same"),
        evidence("rare", "rare_marker details", path_b64="cmFyZQ=="),
    )
    chunks = split_evidence(source, Limits())
    seed = Seed(
        title="tag dedup",
        rationale="Synthetic",
        evidence_ids=[chunks[1].id],
        search_terms=["backend", "rare_marker"],
    )
    old = ExperienceGraph(CheckedModel())
    new = ExperienceGraph(CheckedModel(), plan_requests=True)
    legacy = old._related([seed], chunks)
    prioritized = new._related([seed], chunks)
    assert {c.id for c in prioritized} == {c.id for c in legacy}
    assert prioritized[0].id == chunks[1].id
    assert (
        new.retrieval_summary["weak_only_chunks"] == 2
        and new.retrieval_summary["dropped_chunks"] == 0
    )


def test_fitting_completed_requests_replay_exactly_under_the_new_planner():
    source = inputs(evidence("one", "counter evidence"), evidence("two", "counter second"))
    cp = UnitResponseJournal([], lambda *args: None)
    before = ExperienceGraph(CheckedModel(), isolate_failures=True, checkpoints=cp).run(source)

    class NoCalls(CheckedModel):
        def complete(self, *args):
            pytest.fail("A fitting identical completed request must not be called again")

    after = ExperienceGraph(
        NoCalls(), isolate_failures=True, checkpoints=cp, plan_requests=True
    ).run(source)
    assert after.candidates == before.candidates


def test_claim_and_assessment_planning_preserves_every_index():
    class Many(CheckedModel):
        def complete(self, task, instruction, chunks, context, schema):
            if task == "reconstruct":
                self.calls.append((task, tuple(chunks), json.loads(context)))
                return Reconstruction(
                    claims=[
                        Claim(
                            section="implementation",
                            statement=f"{i}:" + "한글" * 500,
                            status="EVIDENCED",
                            citations=[
                                Citation(evidence_id=chunks[0].id, quote="counter", locator=None)
                            ],
                        )
                        for i in range(50)
                    ]
                )
            return super().complete(task, instruction, chunks, context, schema)

    model = Many()
    events = []
    result = ExperienceGraph(
        model, isolate_failures=True, plan_requests=True, events=lambda *event: events.append(event)
    ).run(inputs(evidence("one", "counter evidence")))
    assert result.coverage["analysis_status"] == "complete"
    audits = [context for task, _, context in model.calls if task == "audit"]
    assessed = [context for task, _, context in model.calls if task == "assess"]
    assert len(audits) > 1 and len(assessed) > 1
    assert [c["statement"] for context in audits for c in context] == [
        f"{i}:" + "한글" * 500 for i in range(50)
    ]
    assert (
        len(result.candidates[0].claims) == 59
    )  # 50 implementation claims + nine explicit UNKNOWN sections.
    proof = next(e[3] for e in events if e[0] == "candidate_validated")
    assert len(proof["audit_pairs"][0]["audits"]) == len(audits)
    assert sum(p["count"] for p in proof["audit_pairs"][0]["audits"]) == 50


def test_impossible_slice_is_not_accepted_as_an_original_chunk():
    chunks = split_evidence(inputs(evidence("one", "counter evidence")), Limits())
    with pytest.raises(CollectionError) as error:
        resolve_chunks(chunks, [chunks[0].id + "/slice/0:99999"])
    assert error.value.code == "analysis_input_corrupt"


def test_recorded_compatibility_proves_exact_requests_and_rejects_missing_or_changed_proof():
    from copy import deepcopy

    from project_log.analysis_checkpoints import contract_digest, resume_rows
    from project_log.analysis_execution import execution_profile
    from project_log.analysis_input import canonical
    from project_log.policy import sha256

    source = inputs(evidence("one", "counter evidence"), evidence("two", "counter second"))
    journal = UnitResponseJournal([], lambda *args: None)
    events = []

    def record(kind, phase, unit, data):
        events.append(
            dict(event_seq=len(events) + 1, kind=kind, phase=phase, unit_digest=unit, data=data)
        )

    ExperienceGraph(CheckedModel(), isolate_failures=True, checkpoints=journal, events=record).run(
        source
    )
    new = execution_profile("synthetic-offline")
    old = {k: v for k, v in new.items() if k != "planning_policy"}
    rows = [
        {
            **cp,
            "ordinal": i,
            "contract_digest": contract_digest(source.digest, old),
            "response_digest": sha256(canonical(cp["response"]).encode()),
            "_origin_unit_digest": cp["request_digest"],
        }
        for i, cp in enumerate(journal.rows, 1)
    ]
    assert len(resume_rows(rows, source, old, new, proof_events=events)) == len(rows)
    for field in ("context", "schema", "chunks", "discovery_ordinal", "event_order", "missing"):
        bad = deepcopy(events)
        start = next(e for e in bad if e["kind"] == "unit_started")
        if field == "event_order":
            start["event_seq"] = len(bad) + 1
        elif field == "missing":
            bad.remove(start)
        else:
            start["data"][field] = {"unexpected": True} if field != "discovery_ordinal" else 999
        with pytest.raises(CollectionError) as error:
            resume_rows(rows, source, old, new, proof_events=bad)
        assert error.value.code == "analysis_checkpoint_invalid"
    with pytest.raises(CollectionError):
        resume_rows(rows, source, old, {**new, "planning_policy": "unknown"}, proof_events=events)


def test_fragmentation_does_not_shift_unchanged_relationship_support_identity():
    from project_log.experiences import fragment_chunk

    source = inputs(*(evidence(str(i), f"counter {i}") for i in range(3)))
    original = split_evidence(source, Limits())
    seeds = [
        Seed(title=str(i), rationale="synthetic", evidence_ids=[c.id], search_terms=["counter"])
        for i, c in enumerate(original[1:])
    ]
    old = ExperienceGraph(CheckedModel())
    new = ExperienceGraph(CheckedModel(), plan_requests=True)
    old.seed_ids = new.seed_ids = ["seed_000000", "seed_000001"]
    expected = old._review_contexts(dict(chunks=original, seeds=seeds), [[0, 1]])
    fragments = (fragment_chunk(original[0], 0, 4), fragment_chunk(original[0], 3, 8))
    new.chunk_positions = {c.id: i for i, c in enumerate(original + fragments)}
    actual = new._review_contexts(dict(chunks=fragments + original[1:], seeds=seeds), [[0, 1]])
    assert actual == expected  # Same body/digest and relationship budget identity.


def test_minimum_unrepresentable_relationship_is_retained_but_never_dispatched():
    from project_log.experience_relations import review_schema

    source = inputs(*(evidence(str(i), f"counter {i}", filename="한글" * 20000) for i in range(2)))
    chunks = split_evidence(source, Limits())
    seeds = [
        Seed(title=str(i), rationale="synthetic", evidence_ids=[c.id], search_terms=["counter"])
        for i, c in enumerate(chunks)
    ]
    model = CheckedModel()
    events = []
    graph = ExperienceGraph(
        model, plan_requests=True, isolate_failures=True, events=lambda *e: events.append(e)
    )
    graph.seed_ids = ["seed_000000", "seed_000001"]
    state = dict(chunks=chunks, seeds=seeds, calls=0)
    contexts = graph._review_contexts(state, [[0, 1]])
    assert len(contexts) == 1 and len(contexts[0]["seeds"]) == 2
    context = contexts[0]
    schema = review_schema(graph.seed_ids, [s["id"] for s in context["supports"]])
    assert (
        graph._unit(state, "reconcile", TASK_INSTRUCTIONS["reconcile"], (), context, schema) is None
    )
    assert model.calls == [] and graph.fresh_calls == 0 and graph.unit_attempts == {}
    assert any(e[0] == "unit_isolated" and e[3]["seed_ids"] == graph.seed_ids for e in events)


def test_consolidation_full_wire_windows_preserve_every_seed():
    seeds = [
        Seed(title=str(i), rationale="한글" * 500, evidence_ids=[str(i)], search_terms=["counter"])
        for i in range(50)
    ]
    model = CheckedModel()
    graph = ExperienceGraph(model, plan_requests=True, limits=Limits(batch_chars=48000))
    graph.seed_ids = [f"seed_{i:06d}" for i in range(len(seeds))]
    result = graph._initial_consolidate(dict(seeds=seeds, calls=0))
    assert sorted(i for group in result["indices"] for i in group) == list(range(50))
    calls = [context for task, _, context in model.calls if task == "consolidate"]
    assert len(calls) > 1
    assert [seed["title"] for context in calls for seed in context] == [str(i) for i in range(50)]


def test_planning_fix_preserves_no_progress_and_daily_wait():
    from datetime import UTC, datetime

    from project_log.analysis_execution import execution_profile
    from project_log.analysis_recovery import resume_block

    new = execution_profile("synthetic-offline")
    old = {k: v for k, v in new.items() if k != "planning_policy"}
    now = datetime(2027, 1, 15, 12, tzinfo=UTC)
    original = dict(
        id="three",
        project_id="synthetic",
        finished_at=now,
        resume_from_request_id="two",
        consent={"descriptor": {"profile": old}},
        error_code="analysis_request_contract_limit_text_bytes",
    )

    class History:
        def execute(self, sql, params=()):
            self.sql, self.params = sql, params
            return self

        def fetchone(self):
            if "count(*)" in self.sql:
                return {"n": 0}
            if "SELECT id,finished_at,consent,resume_from_request_id" in self.sql:
                return {**original, "id": self.params[1], "resume_from_request_id": "one"}
            return None

        def fetchall(self):
            return []

    assert resume_block(History(), original, new, now=now) == "analysis_resume_no_progress"
    assert (
        resume_block(History(), {**original, "error_code": "gemini_free_daily_limit"}, new, now=now)
        == "analysis_resume_daily_wait"
    )


def test_seed_context_is_not_reduced_to_avoid_splitting_a_nearly_full_batch():
    source = inputs(*(evidence(str(i), "한글" * 2150 + str(i)) for i in range(4)))
    chunks = split_evidence(source, Limits())
    group = [
        Seed(title=str(i), rationale="한글" * 500, evidence_ids=[c.id], search_terms=["counter"])
        for i, c in enumerate(chunks)
    ]
    graph = ExperienceGraph(CheckedModel(), plan_requests=True)
    expected = [s.model_dump() for s in group]
    assert graph._fits("reconstruct", (), expected, Reconstruction)
    assert not graph._fits("reconstruct", chunks, expected, Reconstruction)

    def context(part):
        return graph._reconstruction_context(group, [str(i) for i in range(4)], part)

    assert context(chunks) == expected
    work = graph._chunk_work("reconstruct", [chunks], context, lambda part: Reconstruction)
    assert len(work) > 1
    assert all(context(part) == expected for part in work)
    assert [c.id for part in work for c in part] == [c.id for c in chunks]
