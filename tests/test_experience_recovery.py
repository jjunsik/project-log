"""Failure units and recovery use invented evidence; no external provider calls."""

import json
from collections import Counter

import pytest
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_checkpoints import UnitResponseJournal
from project_log.analysis_input import canonical
from project_log.experiences import ExperienceGraph, Limits
from project_log.git import CollectionError


class PairGraph(ExperienceGraph):
    def _review_contexts(self, state, initial):
        contexts = super()._review_contexts(state, initial)
        if not contexts:
            return []
        full = contexts[0]
        result = []
        for left, right in zip(self.seed_ids, self.seed_ids[1:], strict=False):
            seeds = [s for s in full["seeds"] if s["seed_id"] in {left, right}]
            refs = {r for s in seeds for r in s["support_ids"]}
            result.append(
                {
                    **full,
                    "seeds": seeds,
                    "supports": [s for s in full["supports"] if s["id"] in refs],
                }
            )
        return result


class NoRelations(ExperienceGraph):
    def _related(self, group, chunks):
        refs = {ref for seed in group for ref in seed.evidence_ids}
        return [c for c in chunks if c.id in refs]

    def _review_contexts(self, state, initial):
        return []


def journal():
    persisted = []
    return UnitResponseJournal([], lambda *args: persisted.append(args)), persisted


def snapshot():
    return inputs(*(evidence(str(i), f"counter evidence {i}") for i in range(3)))


def test_failed_relation_retains_both_candidates_and_overlapping_valid_relation():
    class InvalidPair(OfflineModel):
        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            if task == "reconcile" and "seed_000000" in json.loads(context)["seeds"][0]["seed_id"]:
                result.grounds = []
            return result

    model = InvalidPair()
    cp, saved = journal()
    result = PairGraph(model, checkpoints=cp, isolate_failures=True).run(snapshot())
    assert result.coverage["analysis_status"] == "partial"
    assert [c.seed_ids for c in result.candidates] == [
        ["seed_000000"],
        ["seed_000001", "seed_000002"],
    ]
    assert result.coverage["consolidation"]["final_groups"] == [
        c.seed_ids for c in result.candidates
    ]
    assert len(result.coverage["unresolved"]) == 1
    assert result.coverage["unresolved"][0]["seed_ids"] == ["seed_000000", "seed_000001"]
    assert Counter(t for t, _, _ in model.calls)["reconcile"] == 3
    assert len([r for r in saved if r[1] == "reconcile"]) == 1
    assert all(c.review_status == "unreviewed" for c in result.candidates)


@pytest.mark.parametrize("stage", ["reconstruct", "audit", "assess"])
def test_failed_candidate_unit_recovers_after_independent_candidates_without_recalling_success(
    stage,
):
    class Once(OfflineModel):
        failed = False

        def complete(self, task, instruction, chunks, context, schema):
            result = super().complete(task, instruction, chunks, context, schema)
            if task == stage and not self.failed:
                self.failed = True
                raise CollectionError("unsupported_claim", "Synthetic invalid unit")
            return result

    cp, saved = journal()
    model = Once()
    result = NoRelations(model, checkpoints=cp, isolate_failures=True).run(snapshot())
    assert result.coverage["analysis_status"] == "complete"
    assert len(result.candidates) == 3
    assert result.coverage["unresolved"] == []
    assert len(result.coverage["recovered_unit_ids"]) == 1
    assert len({(r[1], r[2]) for r in saved}) == len(saved)
    # Independent candidate 2 completes before the failed candidate's second attempt.
    attempts = [i for i, (task, _, _) in enumerate(model.calls) if task == stage]
    assert len(attempts) == 4
    assert attempts[-1] > attempts[2]


def test_recovered_discovery_rebuilds_relationships_between_recovered_items_and_keeps_all_seeds():
    class Later(OfflineModel):
        seen = Counter()

        def complete(self, task, instruction, chunks, context, schema):
            if task == "discover":
                key = chunks[0].id
                self.seen[key] += 1
                if self.seen[key] == 1 and chunks[0].text.endswith(("0", "1")):
                    self.calls.append((task, chunks, {}))
                    raise CollectionError("invalid_discovery_reference_out_of_batch", "Synthetic")
            return super().complete(task, instruction, chunks, context, schema)

    model = Later()
    cp, _ = journal()
    source = inputs(*(evidence(str(i), "counter " + "x" * 700 + str(i)) for i in range(3)))
    limits = Limits(chunk_chars=1000, batch_chars=1500)
    result = ExperienceGraph(model, limits, checkpoints=cp, isolate_failures=True).run(source)
    assert result.coverage["analysis_status"] == "complete"
    assert len(result.coverage["recovered_unit_ids"]) == 2
    assert len(result.coverage["consolidation"]["seeds"]) == 3
    assert sorted(s for c in result.candidates for s in c.seed_ids) == [
        f"seed_{i:06d}" for i in range(3)
    ]
    assert any(t == "reconstruct" for t, _, _ in model.calls[:4])
    assert any(t == "reconcile" and len(c["seeds"]) == 3 for t, _, c in model.calls)

    class NoCalls(OfflineModel):
        def complete(self, *args):
            pytest.fail("Recovered discovery must retain global IDs and exact requests on Resume")

    replay = ExperienceGraph(NoCalls(), limits, checkpoints=cp, isolate_failures=True).run(source)
    assert replay.candidates == result.candidates
    assert replay.coverage["consolidation"] == result.coverage["consolidation"]
    assert (
        result.coverage["recovery_passes"][0]["seeds"][0]["seed_key"]
        == replay.coverage["consolidation"]["seeds"][0]["seed_key"]
    )


def test_relation_recovery_regroups_recovered_candidates_and_rebuilds_merged_result():
    class Once(OfflineModel):
        relations = 0

        def complete(self, task, *args):
            result = super().complete(task, *args)
            if task == "reconcile":
                self.relations += 1
                if self.relations == 1:
                    result.grounds = []
            return result

    model = Once()
    result = ExperienceGraph(model, isolate_failures=True).run(snapshot())
    assert result.coverage["analysis_status"] == "complete"
    assert len(result.candidates) == 1
    assert len(result.candidates[0].seed_ids) == 3
    assert len(result.coverage["recovered_unit_ids"]) == 1
    assert Counter(t for t, _, _ in model.calls)["reconstruct"] == 4


def test_budget_exhaustion_preserves_valid_units_and_marks_unprocessed_work_without_more_sends():
    model = OfflineModel()
    result = NoRelations(model, Limits(max_calls=5), isolate_failures=True).run(snapshot())
    assert result.coverage["analysis_status"] == "partial"
    assert result.coverage["stop_code"] == "analysis_budget_exceeded"
    assert len(model.calls) == 5
    assert result.coverage["unresolved"]


@pytest.mark.parametrize(
    "code",
    [
        "analysis_stopped",
        "analysis_wire_mismatch",
        "analysis_consent_mismatch",
        "provider_authentication",
        "provider_invalid_request",
        "provider_read_error_body_partial_h200",
    ],
)
def test_global_guards_and_unknown_transport_are_not_retried_as_failed_units(code):
    class Stop(OfflineModel):
        def complete(self, *args):
            raise CollectionError(code, "Synthetic global guard")

    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(Stop(), isolate_failures=True).run(snapshot())
    assert exc.value.code == code


def test_recovery_uses_exact_request_journal_and_does_not_mutate_stored_claims():
    cp, _ = journal()
    first = ExperienceGraph(OfflineModel(), checkpoints=cp, isolate_failures=True).run(snapshot())
    before = canonical(cp.rows)

    class NoCalls(OfflineModel):
        def complete(self, *args):
            pytest.fail("Validated responses must not be called again")

    second = ExperienceGraph(NoCalls(), checkpoints=cp, isolate_failures=True).run(snapshot())
    assert second == first
    assert canonical(cp.rows) == before


def test_known_notice_and_isolation_transition_replays_only_exact_unchanged_requests():
    from project_log.analysis_checkpoints import (
        ISOLATION_COMPATIBILITY,
        compatibility_policy,
        contract_digest,
        resume_rows,
    )
    from project_log.analysis_execution import execution_profile
    from project_log.policy import sha256

    new = execution_profile("synthetic-offline")
    old = {k: v for k, v in new.items() if k != "failure_policy"}
    cp, _ = journal()
    source = snapshot()
    ExperienceGraph(OfflineModel(), checkpoints=cp, isolate_failures=True).run(source)
    rows = [
        {
            **r,
            "ordinal": i,
            "contract_digest": contract_digest(source.digest, old),
            "response_digest": sha256(canonical(r["response"]).encode()),
        }
        for i, r in enumerate(cp.rows, 1)
    ]
    assert compatibility_policy(old, new) == ISOLATION_COMPATIBILITY
    before = canonical(rows)
    reused = resume_rows(rows, source, old, new)
    assert [r["task"] for r in reused] == [r["task"] for r in rows]
    assert all(r["contract_digest"] == contract_digest(source.digest, new) for r in reused)
    assert canonical(rows) == before
    for key, value in [
        ("model", "unreviewed"),
        ("notice_version", "unreviewed"),
        ("instruction_digest", "0" * 64),
        ("limits", {**old["limits"], "max_calls": 513}),
    ]:
        with pytest.raises(CollectionError):
            resume_rows(rows, source, {**old, key: value}, new)
    with pytest.raises(CollectionError):
        resume_rows([{**rows[0], "request_digest": "0" * 64}, *rows[1:]], source, old, new)
    with pytest.raises(CollectionError):
        resume_rows(rows[::-1], source, old, new)


def test_partial_resume_preserves_relation_cap_and_no_progress_guard(db, tmp_path):
    from test_analysis_admission import submission
    from test_analysis_resume import prepare_resume
    from test_analysis_worker import accepted, setup_worker, stop, terminal

    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "grounding_merge_missing")
    store.add(pid, "second.md", b"Another synthetic counter correction")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        first = terminal(worker, pid, request)
        assert first["error_code"] == "analysis_partial"
        calls = db.all("SELECT * FROM analysis_calls WHERE phase='reconcile' ORDER BY entered_at")
        assert len(calls) == 2
        for _ in range(3):
            prep = prepare_resume(worker, pid, cid, request)
            assert prep["state"] == "ready"
            request = str(worker.admission.submit(pid, submission(prep))["id"])
            done = terminal(worker, pid, request)
            assert done["state"] == "failed" and done["error_code"] == "analysis_partial"
            assert done["call_seq"] == 0
        assert (
            prepare_resume(worker, pid, cid, request)["error_code"] == "analysis_resume_no_progress"
        )
        assert (
            db.all("SELECT * FROM analysis_calls WHERE phase='reconcile' ORDER BY entered_at")
            == calls
        )
        assert db.one("SELECT result_partial FROM analysis_requests WHERE id=%s", (request,))[
            "result_partial"
        ]
    finally:
        stop(worker, owner)
