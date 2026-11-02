"""Durable execution evidence, no live Gemini or production database access."""

import json
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_analysis_api import body, prepare, setup, submit
from test_analysis_api import terminal as api_terminal
from test_analysis_worker import accepted, setup_worker, stop, terminal, wait_for

from project_log.analysis_events import AnalysisFault
from project_log.api import create_app
from project_log.experience_store import Experiences
from project_log.git import CollectionError


def events(db, request):
    return db.all(
        "SELECT * FROM analysis_events WHERE request_id=%s ORDER BY event_seq", (request,)
    )


def test_graph_rejections_and_both_passes_survive_an_unsuccessful_execution(db, tmp_path):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "stage_audit")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "failed" and done["error_code"] == "analysis_partial"
        journal = events(db, request)
        assert [e["data"]["pass"] for e in journal if e["kind"] == "pass_started"] == [0, 1]
        rejected = [e for e in journal if e["kind"] == "unit_failed"]
        assert len(rejected) == 2
        assert all(e["data"]["code"] == "claim_audit_incomplete" for e in rejected)
        assert all(e["data"]["response"] == {"findings": []} for e in rejected)
        assert all(e["data"]["cause_status"] == "CONFIRMED" for e in rejected)
        assert not [e for e in journal if e["kind"] == "candidate_validated"]
        assert journal[-1]["kind"] == "execution_finished"
        assert [e["event_seq"] for e in journal] == list(range(1, len(journal) + 1))
    finally:
        stop(worker, owner)


@pytest.mark.parametrize("failure", ["file_size", "digest", "state", "persist"])
def test_finalization_fault_retains_precise_condition_and_only_certified_partial_candidate(
    db, tmp_path, monkeypatch, failure
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path)
    original_read = worker.files.read
    original_insert = Experiences.insert
    failed = False

    def read(token, maximum, *, suffix=".json"):
        nonlocal failed
        raw = original_read(token, maximum, suffix=suffix)
        if suffix == ".json" and failure != "persist" and not failed:
            failed = True
            if failure == "file_size":
                raise AnalysisFault(
                    "analysis_result_file_too_large",
                    "result_read",
                    "bytes_gt_limit",
                    measured_bytes=maximum + 1,
                    limit_bytes=maximum,
                )
            if failure == "digest":
                return b"{}"
            value = json.loads(raw)
            value["coverage"]["analysis_status"] = "partial"
            assert worker.child
            raw = json.dumps(value).encode()
            from project_log.policy import sha256

            worker.child.result_digest = sha256(raw)
        return raw

    def insert(self, *args, **kwargs):
        nonlocal failed
        if failure == "persist" and not failed:
            failed = True
            raise psycopg.errors.CheckViolation("Synthetic final insert rejection")
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(worker.files, "read", read)
    monkeypatch.setattr(Experiences, "insert", insert)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        codes = {
            "file_size": "analysis_result_file_too_large",
            "digest": "analysis_result_digest_mismatch",
            "state": "analysis_result_state_invalid",
            "persist": "analysis_result_persist_failed",
        }
        assert done["state"] == "failed" and done["error_code"] == codes[failure], done
        assert done["result_partial"] and done["result_run_id"]
        result = Experiences(db).detail(pid, str(done["result_run_id"]))["result"]
        assert result["coverage"]["finalization_fallback"]
        assert result["coverage"]["analysis_status"] == "partial"
        assert result["coverage"]["graph_status"] == "complete"
        assert result["coverage"]["unresolved_total"] == 1
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["review_status"] == "unreviewed"
        journal = events(db, request)
        proof = next(e["data"] for e in journal if e["kind"] == "candidate_validated")
        assert (
            proof["required_units"]["reconstruct"]
            and proof["required_units"]["audit"]
            and proof["required_units"]["assess"]
        )
        diagnostics = [e["data"] for e in journal if e["kind"] == "finalization_failed"]
        assert diagnostics[-1]["code"] == codes[failure]
        assert diagnostics[-1]["condition"] != "unknown"
        token = str(
            db.one("SELECT owner_token FROM analysis_requests WHERE id=%s", (request,))[
                "owner_token"
            ]
        )
        assert worker.files.path(token, ".failure.jsonl").is_file()
        assert worker.files.path(token, ".json").is_file()
        assert (
            db.one(
                "SELECT count(*) AS n FROM analysis_checkpoints WHERE request_id=%s", (request,)
            )["n"]
            == 4
        )
    finally:
        stop(worker, owner)


def test_global_quota_stops_at_recorded_position_and_preserves_prior_complete_candidate(
    db, tmp_path
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "durable_quota_after_candidate")
    store.add(pid, "second.md", b"Another synthetic counter correction")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["error_code"] == "gemini_free_daily_limit" and done["result_partial"]
        result = Experiences(db).detail(pid, str(done["result_run_id"]))["result"]
        assert len(result["candidates"]) == 1
        assert result["coverage"]["pending_work"]
        journal = events(db, request)
        assert len([e for e in journal if e["kind"] == "candidate_validated"]) == 1
        assert len([e for e in journal if e["kind"] == "execution_paused"]) == 1
        assert [e["data"]["status"] for e in journal if e["kind"] == "pass_finished"] == ["paused"]
        assert (
            len(
                [
                    e
                    for e in journal
                    if e["kind"] == "unit_failed"
                    and e["data"].get("code") == "gemini_free_daily_limit"
                ]
            )
            == 1
        )
        assert [e for e in journal if e["kind"] == "execution_paused"][0]["data"][
            "unattempted_are_failures"
        ] is False
    finally:
        stop(worker, owner)


def test_recovered_and_initial_candidates_link_to_the_latest_partition(db, tmp_path):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "durable_recover_audit")
    store.add(pid, "second.md", b"Independent synthetic correction")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "completed", done
        journal = events(db, request)
        plans = [e for e in journal if e["kind"] == "partition"]
        assert [e["data"]["pass"] for e in plans] == [0, 1]
        failures = [e for e in journal if e["kind"] == "unit_failed"]
        recovered = [e for e in journal if e["kind"] == "unit_recovered"]
        assert failures[0]["data"]["code"] == "claim_audit_incomplete" and recovered
        certificates = [
            e
            for e in journal
            if e["kind"] == "candidate_validated" and e["event_seq"] > plans[-1]["event_seq"]
        ]
        assert len(certificates) == 2 and any(e["data"].get("reused") for e in certificates)
        result = Experiences(db).detail(pid, str(done["result_run_id"]))["result"]
        assert {c["id"] for c in result["candidates"]} == {
            e["data"]["candidate"]["id"] for e in certificates
        }
    finally:
        stop(worker, owner)


def test_relation_grounding_rejection_and_conditional_recovery_are_durable(db, tmp_path):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "grounding_merge_once")
    store.add(pid, "second.md", b"Related synthetic correction")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "completed", done
        journal = events(db, request)
        rejected = next(e for e in journal if e["data"].get("condition") == "relation_grounding")
        assert rejected["data"]["response"]["grounds"] == []
        assert len(rejected["data"]["context"]["seeds"]) == 2
        assert rejected["data"]["code"].startswith("candidate_relation_grounding_merge_labels_")
        retry = next(e for e in journal if e["kind"] == "attempt_retry_scheduled")
        assert retry["data"]["reason"] == "reviewed_grounding_regeneration_within_shared_budget"
        calls = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s AND phase='reconcile' ORDER BY seq",
            (request,),
        )
        assert len(calls) == 2 and all(c["outcome"] == "responded" for c in calls)
        assert any(e["kind"] == "unit_validated" and e["phase"] == "reconcile" for e in journal)
    finally:
        stop(worker, owner)


def test_missing_durable_ack_prevents_calls_and_retains_the_pending_event_file(
    db, tmp_path, monkeypatch
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path)

    def reject(*args):
        raise CollectionError("synthetic_event_write_failed", "Synthetic commit failure")

    monkeypatch.setattr(worker.state, "journal", reject)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["error_code"] == "analysis_event_persist_failed" and done["call_seq"] == 0
        token = str(
            db.one("SELECT owner_token FROM analysis_requests WHERE id=%s", (request,))[
                "owner_token"
            ]
        )
        assert worker.files.path(token, ".event.json").is_file()
        assert (
            json.loads(worker.files.path(token, ".event.json").read_text())["kind"]
            == "pass_started"
        )
        assert any(e["kind"] == "finalization_failed" for e in events(db, request))
    finally:
        stop(worker, owner)


def test_interruption_retains_certified_candidate_and_records_unknown_cause(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(
        db, tmp_path, "durable_block_after_candidate"
    )
    store.add(pid, "second.md", b"Another synthetic counter correction")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(
            lambda: (
                db.one(
                    "SELECT count(*) AS n FROM analysis_events WHERE request_id=%s "
                    "AND kind='candidate_validated'",
                    (request,),
                )["n"]
                == 1
            )
        )
        wait_for(
            lambda: (
                db.one(
                    "SELECT count(*) AS n FROM analysis_calls WHERE request_id=%s AND "
                    "phase='reconstruct'",
                    (request,),
                )["n"]
                == 2
            )
        )
        worker.close()
        done = worker.admission.request(pid, request)
        assert done["state"] == "failed" and done["result_partial"]
        assert (
            len(Experiences(db).detail(pid, str(done["result_run_id"]))["result"]["candidates"])
            == 1
        )
        assert any(
            e["kind"] == "execution_interrupted" and e["data"]["cause_status"] == "UNKNOWN"
            for e in events(db, request)
        )
    finally:
        (directory / "release").touch()
        owner.close()


def test_event_api_and_partial_results_are_project_scoped_paged_and_no_store(db, tmp_path):
    pid, cid, _, mode_file, options = setup(db, tmp_path)
    mode_file.write_text("success")
    with TestClient(create_app(db, **options)) as client:
        row = submit(client, pid, body(prepare(client, pid, cid)))
        done = api_terminal(client, pid, row["id"])
        assert done["state"] == "completed"
        base = f"/api/projects/{pid}/analysis/requests/{row['id']}"
        response = client.get(base + "/events?limit=2")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        page = response.json()
        assert len(page["events"]) == 2 and page["total"] > 2
        assert "context" not in page["events"][1]["data"]
        next_page = client.get(base + f"/events?after={page['next_after']}&limit=2").json()
        assert next_page["events"][0]["event_seq"] == 3
        detailed = client.get(base + "/events/1")
        assert detailed.status_code == 200 and detailed.headers["cache-control"] == "no-store"
        partial = client.get(base + "/partial").json()
        assert partial["stage_verified"] and not partial["complete_analysis"]
        assert partial["graph_summary"]["status"] == "complete"
        assert partial["graph_summary"]["unresolved"] == []
        assert partial["total"] == 1
        other = str(uuid4())
        assert (
            client.get(f"/api/projects/{other}/analysis/requests/{row['id']}/events").status_code
            == 404
        )
        assert (
            client.get(f"/api/projects/{other}/analysis/requests/{row['id']}/partial").status_code
            == 404
        )


@pytest.mark.parametrize("tamper", ["audit", "claim", "relation_membership"])
def test_uncertified_candidate_or_relation_cannot_be_presented_as_partial_experience(
    db, tmp_path, monkeypatch, tamper
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path)
    original = worker.state.journal

    def journal(project, request, token, payload):
        copied = json.loads(json.dumps(payload))
        if copied["kind"] == "candidate_validated" and tamper in {"audit", "claim"}:
            if tamper == "audit":
                copied["data"]["required_units"]["audit"] = []
            else:
                copied["data"]["candidate"]["claims"][0]["statement"] = "Invented unverified claim"
        if copied["kind"] == "partition" and tamper == "relation_membership":
            copied["data"]["groups"][0]["seed_keys"] = ["invented"]
        return original(project, request, token, copied)

    monkeypatch.setattr(worker.state, "journal", journal)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "failed" and done["error_code"] == "analysis_event_persist_failed"
        assert done["result_run_id"] is None
        assert not [e for e in events(db, request) if e["kind"] == "candidate_validated"]
        assert Experiences(db).listing(pid) == []
    finally:
        stop(worker, owner)


def test_real_serialized_size_guard_keeps_original_limit_and_reports_exact_measurement():
    from project_log.analysis_child import serialize_result
    from project_log.analysis_execution import execution_profile
    from project_log.experiences import ANALYSIS_POLICY, AnalysisResult

    limit = execution_profile("synthetic")["result_max_bytes"]
    assert limit == 32 * 1024 * 1024
    result = AnalysisResult(
        input_digest="synthetic",
        policy=ANALYSIS_POLICY,
        candidates=[],
        coverage={"body": "x" * limit},
        model_calls=0,
    )
    with pytest.raises(AnalysisFault) as exc:
        serialize_result(result, limit)
    assert exc.value.code == "analysis_result_too_large"
    assert exc.value.details["stage"] == "result_size"
    assert exc.value.details["condition"] == "serialized_bytes_gt_limit"
    assert exc.value.details["limit_bytes"] == limit
    assert exc.value.details["measured_bytes"] == len(result.model_dump_json().encode())


def test_unclassified_serialization_cause_stays_unknown():
    from project_log.analysis_child import serialize_result
    from project_log.experiences import ANALYSIS_POLICY, AnalysisResult

    result = AnalysisResult(
        input_digest="synthetic",
        policy=ANALYSIS_POLICY,
        candidates=[],
        coverage={"value": object()},
        model_calls=0,
    )
    with pytest.raises(AnalysisFault) as exc:
        serialize_result(result, 32 * 1024 * 1024)
    assert exc.value.code == "analysis_result_serialize_failed"
    assert exc.value.details["cause_status"] == "UNKNOWN"


def test_durable_resume_requires_exact_profile_and_origin_and_retains_historical_daily_stop():
    from datetime import UTC, datetime, timedelta

    from test_experiences import OfflineModel, evidence, inputs

    from project_log.analysis_checkpoints import (
        DURABILITY_COMPATIBILITY,
        UnitResponseJournal,
        compatibility_policy,
        contract_digest,
        resume_rows,
    )
    from project_log.analysis_execution import execution_profile
    from project_log.analysis_input import canonical
    from project_log.analysis_recovery import resume_block
    from project_log.experiences import ExperienceGraph
    from project_log.policy import sha256

    new = execution_profile("synthetic")
    old = {k: v for k, v in new.items() if k != "durability_policy"}
    source = inputs(evidence("one", "Synthetic counter correction"))
    cp = UnitResponseJournal([], lambda *args: None)
    ExperienceGraph(OfflineModel(), checkpoints=cp, isolate_failures=True).run(source)
    rows = [
        {
            **r,
            "ordinal": i,
            "contract_digest": contract_digest(source.digest, old),
            "response_digest": sha256(canonical(r["response"]).encode()),
            "_origin_unit_digest": r["request_digest"],
        }
        for i, r in enumerate(cp.rows, 1)
    ]
    assert compatibility_policy(old, new) == DURABILITY_COMPATIBILITY
    reused = resume_rows(rows, source, old, new)
    assert len(reused) == len(rows)
    assert all(r["contract_digest"] == contract_digest(source.digest, new) for r in reused)
    with pytest.raises(CollectionError):
        resume_rows([{**rows[0], "_origin_unit_digest": "0" * 64}, *rows[1:]], source, old, new)
    with pytest.raises(CollectionError):
        compatibility_policy(old, {**new, "model": "unreviewed"})
    now = datetime(2027, 1, 15, 12, tzinfo=UTC)
    original = {
        "id": "synthetic",
        "error_code": "analysis_result_invalid",
        "finished_at": now,
        "consent": {"descriptor": {"profile": old}},
    }

    class Daily:
        def execute(self, *args):
            return self

        def fetchone(self):
            return {"error_code": "gemini_free_daily_limit"}

    assert (
        resume_block(Daily(), original, new, now=now + timedelta(hours=1))
        == "analysis_resume_daily_wait"
    )
    assert resume_block(Daily(), original, new, now=now + timedelta(days=1)) is None


def test_wire_planned_audits_certify_every_claim_and_reject_a_missing_slice(db, tmp_path):
    from copy import deepcopy

    from project_log.analysis_events import validate_candidate

    pid, cid, _, owner, worker, _ = setup_worker(db, tmp_path, "planning_many_claims")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "completed", done
        certificate = next(
            e["data"] for e in events(db, request) if e["kind"] == "candidate_validated"
        )
        assert len(certificate["audit_pairs"][0]["audits"]) > 1
        assert sum(p["count"] for p in certificate["audit_pairs"][0]["audits"]) == 50
        bad = deepcopy(certificate)
        bad["audit_pairs"][0]["audits"].pop()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM analysis_requests WHERE id=%s", (request,)).fetchone()
            validate_candidate(conn, row, certificate)
            with pytest.raises(CollectionError) as error:
                validate_candidate(conn, row, bad)
            assert error.value.code == "analysis_event_invalid"
    finally:
        stop(worker, owner)


def test_wire_planned_slices_do_not_relax_claim_schema(db, tmp_path, monkeypatch):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path)
    worker.profile["limits"].update(chunk_chars=48000, batch_chars=48000)
    worker.admission.profile["limits"].update(chunk_chars=48000, batch_chars=48000)
    store.add(pid, "long.md", ("한글" * 15000 + " counter").encode())
    try:
        request, _ = accepted(worker, pid, cid)
        # This fake model uses the whole text as a claim and exceeds Claim's 4k
        # statement contract. A schema rejection must retain the split work, never
        # produce a completed candidate or relax the claim validation.
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "failed", done
        assert not [e for e in events(db, request) if e["kind"] == "candidate_validated"]
        assert any(
            e["kind"] == "unit_started"
            and any("/slice/" in key for key in e["data"].get("chunk_ids", []))
            for e in events(db, request)
        )
    finally:
        stop(worker, owner)


def test_wire_planned_fragments_keep_immutable_offsets_in_finalization_fallback_and_api(
    db, tmp_path, monkeypatch
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, "planning_short_claims")
    worker.profile["limits"].update(chunk_chars=48000, batch_chars=48000)
    worker.admission.profile["limits"].update(chunk_chars=48000, batch_chars=48000)
    store.add(pid, "long.md", ("한글" * 15000 + " counter").encode())
    original_read = worker.files.read

    def corrupt_final_once(token, maximum, *, suffix=".json"):
        raw = original_read(token, maximum, suffix=suffix)
        return b"{}" if suffix == ".json" else raw

    monkeypatch.setattr(worker.files, "read", corrupt_final_once)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        done = terminal(worker, pid, request)
        assert done["state"] == "failed" and done["result_partial"], done
        assert done["error_code"] == "analysis_result_digest_mismatch", done
        result = Experiences(db).detail(pid, str(done["result_run_id"]))["result"]
        fragments = [c for c in result["coverage"]["chunks"] if "/slice/" in c["id"]]
        assert fragments and any(c["offset"] > 0 for c in fragments)
        with TestClient(create_app(db, start_worker=False)) as client:
            response = client.get(f"/api/projects/{pid}/analysis/requests/{request}/partial")
            assert response.status_code == 200 and response.json()["total"] == 1
            partial = response.json()
            assert any("/slice/" in c["id"] for c in partial["chunks"])
            reference = next(c["references"][0] for c in partial["chunks"] if "/slice/" in c["id"])
            evidence = client.get(
                f"/api/projects/{pid}/analysis/requests/{request}/partial/evidence",
                params={"reference": reference},
            )
            assert (
                evidence.status_code == 200
                and evidence.json()["body"] == "한글" * 15000 + " counter"
            )
    finally:
        stop(worker, owner)
