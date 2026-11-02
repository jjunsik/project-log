import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from analysis_worker_fixtures import offline_factory
from test_analysis_admission import collection, prepared, submission
from test_materials import project, stored_files

from project_log import analysis_worker
from project_log.analysis_execution import ExecutionState, execution_profile
from project_log.analysis_worker import AnalysisWorker
from project_log.api import create_app
from project_log.experience_store import Experiences
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.service import Projects, Registration
from project_log.worker import AppOwnership, Worker


def wait_for(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail("Timed out waiting for synthetic worker condition")


def setup_worker(db, tmp_path, mode="success", *, control_file=None):
    pid = project(db, tmp_path)
    cid = collection(db, pid)
    store = Materials(db)
    store.add(pid, "context.md", b"Synthetic counter correction evidence")
    directory = tmp_path / ("control-" + uuid4().hex)
    directory.mkdir()
    profile = {
        **execution_profile(
            "gemini-3.1-flash-lite" if mode.startswith("sdk") else "synthetic-offline"
        ),
        "test_directory": str(directory),
        "test_mode": mode,
    }
    if control_file is not None:
        profile["test_control_file"] = str(control_file)
    owner = AppOwnership(db)
    worker = AnalysisWorker(
        db,
        store,
        owner,
        profile=profile,
        model_factory=offline_factory,
    )
    worker.recover()
    worker.state.open()
    return pid, cid, store, owner, worker, directory


def accepted(worker, pid, cid):
    prep = prepared(worker.admission, pid, cid)
    body = submission(prep)
    row = worker.admission.submit(pid, body)
    assert row["state"] == "queued"
    return str(row["id"]), body


def terminal(worker, pid, request):
    def read():
        row = worker.admission.request(pid, request)
        return row if row["state"] in {"completed", "failed", "cancelled"} else None

    # Retry scenarios exercise the real cancellable cooldown, not a shortened policy.
    return wait_for(read, timeout=15 + (worker.profile or {}).get("retry_delay", 0))


def stop(worker, owner):
    try:
        worker.close()
    finally:
        owner.close()


def test_background_frozen_input_results_evidence_replay_and_previous_success(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    try:
        request, body = accepted(worker, pid, cid)
        collection(db, pid)  # not included in accepted input
        worker.start()
        result = terminal(worker, pid, request)
        assert result["state"] == "completed", result
        run = Experiences(db).detail(pid, str(result["result_run_id"]))
        assert run["mode"] == "isolated" and run["result"]["candidates"]
        assert [c["id"] for c in run["input_manifest"]["collections"]] == [cid]
        reference = run["result"]["candidates"][0]["evidence_ids"][0]
        assert (
            Experiences(db).evidence(pid, str(run["id"]), reference)["body"]
            == "Synthetic counter correction evidence"
        )
        assert worker.admission.submit(pid, body)["result_run_id"] == result["result_run_id"]
        assert len(Experiences(db).listing(pid)) == 1
        store.add(pid, "released.md", b"Project unlocked after successful commit")
        # A later failed run preserves the previous complete revision and its evidence.
        worker.factory = None  # isolated default cannot read a key or construct a live client
        next_request, _ = accepted(worker, pid, cid)
        later = terminal(worker, pid, next_request)
        assert later["state"] == "failed"
        assert Experiences(db).detail(pid, str(run["id"])) == run
    finally:
        stop(worker, owner)


@pytest.mark.parametrize(
    "kind",
    [
        "out_of_batch",
        "duplicate",
        "out_of_batch_duplicate",
        "sdk_read_timeout",
        "sdk_body_timeout",
        "sdk_cleanup",
        "stage_consolidate",
        "stage_reconcile",
        "stage_reconstruct",
        "stage_audit",
        "stage_assess",
    ],
)
def test_response_failure_preserves_previous_success_and_bounds_unit_recovery(db, tmp_path, kind):
    control = tmp_path / "model-mode"
    control.write_text("success")
    pid, cid, store, owner, worker, directory = setup_worker(
        db,
        tmp_path,
        mode="sdk_diagnostic" if kind.startswith("sdk") else "success",
        control_file=control,
    )
    try:
        if kind.startswith("stage_"):
            store.add(pid, "second.md", b"Another synthetic counter correction")
        request, _ = accepted(worker, pid, cid)
        worker.start()
        first = terminal(worker, pid, request)
        assert first["state"] == "completed"
        previous = Experiences(db).detail(pid, str(first["result_run_id"]))
        before_collections = db.all("SELECT * FROM collections WHERE project_id=%s", (pid,))
        before_materials = store.listing(pid)
        before_files = {path: (store.root / path).read_bytes() for path in stored_files(store)}
        control.write_text(kind if kind.startswith(("sdk", "stage_")) else f"reference_{kind}")
        failed_request, _ = accepted(worker, pid, cid)
        frozen_before = db.one(
            "SELECT input_payload,input_manifest,consent FROM analysis_requests WHERE id=%s",
            (failed_request,),
        )
        failed = terminal(worker, pid, failed_request)
        assert failed["state"] == "failed"
        diagnostic = {
            "sdk_read_timeout": "provider_read_timeout_transport_hunknown",
            "sdk_body_timeout": "provider_read_timeout_body_partial_h200",
            "sdk_cleanup": "provider_close_error_transport_close_h200",
        }.get(kind)
        stage_errors = {
            "stage_consolidate": "candidate_coverage_invalid_m1_x0_v0_n2",
            "stage_reconcile": "candidate_relation_invalid_m1_x0_v0",
            "stage_reconstruct": "invalid_evidence_reference",
            "stage_audit": "claim_audit_incomplete",
            "stage_assess": "unit_schema_invalid",
        }
        calls = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (failed_request,)
        )
        if diagnostic:
            assert failed["result_run_id"] is None
            assert failed["error_code"] == diagnostic
            assert [call["phase"] for call in calls] == ["discover"]
            assert calls[-1]["outcome"] == "failed" and calls[-1]["error_code"] == diagnostic
            assert json.loads((directory / "quota.json").read_text())["count"] == 1
            assert "private" not in failed["error_code"]
        else:
            from collections import Counter

            assert failed["result_run_id"] is not None
            assert failed["error_code"] == "analysis_partial"
            partial = Experiences(db).detail(pid, str(failed["result_run_id"]))["result"]
            assert partial["coverage"]["analysis_status"] == "partial"
            assert partial["coverage"]["unresolved"]
            expected = stage_errors.get(kind) or f"invalid_discovery_reference_{kind}"
            assert partial["coverage"]["history_request_id"] == failed_request
            assert partial["coverage"]["events_available"]
            assert expected in {
                e["data"]["code"]
                for e in db.all(
                    "SELECT data FROM analysis_events WHERE request_id=%s AND kind='unit_failed'",
                    (failed_request,),
                )
            }
            assert max(Counter(c["unit_request_digest"] for c in calls).values()) <= 2
            assert all(call["outcome"] == "responded" for call in calls)
            invalid_task = kind.removeprefix("stage_") if kind.startswith("stage_") else "discover"
            saved = db.all(
                "SELECT task FROM analysis_checkpoints WHERE request_id=%s", (failed_request,)
            )
            assert all(c["task"] != invalid_task for c in saved)
        assert Experiences(db).detail(pid, str(first["result_run_id"])) == previous
        assert len(Experiences(db).listing(pid)) == (1 if diagnostic else 2)
        assert db.all("SELECT * FROM collections WHERE project_id=%s", (pid,)) == before_collections
        assert store.listing(pid) == before_materials
        assert {
            path: (store.root / path).read_bytes()
            for path in stored_files(store)
            if path.parts[0] != ".analysis-workers"
        } == before_files
        if diagnostic:
            assert list(worker.files.root.glob("*.failure.jsonl"))
        accepted_input = db.one(
            "SELECT input_payload,input_manifest,consent FROM analysis_requests WHERE id=%s",
            (failed_request,),
        )
        assert accepted_input == frozen_before
        assert accepted_input["consent"]["input_digest"] == failed["input_digest"]
        assert not list(worker.files.root.glob("*.json"))
        store.add(pid, "released.md", b"Final reference failure unlocks the project")
    finally:
        stop(worker, owner)


def test_queued_cancel_and_duplicate_cancel_never_dispatch(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    try:
        request, _ = accepted(worker, pid, cid)
        assert worker.state.cancel(pid, request)["state"] == "cancel_requested"
        assert worker.state.cancel(pid, request)["state"] == "cancel_requested"
        with pytest.raises(CollectionError):
            store.add(pid, "blocked.md", b"Not yet cancelled")
        worker.once()
        assert worker.admission.request(pid, request)["state"] == "cancelled"
        assert not list(directory.glob("attempt-*"))
        assert db.all("SELECT * FROM analysis_calls") == []
        assert worker.state.cancel(pid, request)["state"] == "cancelled"
        store.add(pid, "released.md", b"Cancelled final state unlocks")
    finally:
        stop(worker, owner)


def test_running_cancel_kills_blocked_child_before_unlock_and_no_next_call(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "blocked")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(lambda: (directory / "attempt-1").exists())
        handle = worker.child
        assert handle and handle.process.is_alive()
        worker.state.cancel(pid, request)
        with pytest.raises(CollectionError):
            store.add(pid, "blocked.md", b"Cancel intent does not unlock")
        result = terminal(worker, pid, request)
        assert result["state"] == "cancelled" and not handle.process.is_alive()
        assert result["call_seq"] == 1 and len(list(directory.glob("attempt-*"))) == 1
        call = db.one("SELECT * FROM analysis_calls WHERE request_id=%s", (request,))
        assert call["outcome"] == "failed" and call["finished_at"] is not None
        assert call["error_code"] == "analysis_call_outcome_unknown"
        assert Experiences(db).listing(pid) == []
        store.add(pid, "released.md", b"Child termination is confirmed")
    finally:
        stop(worker, owner)


def test_execution_watchdog_allows_120_second_calls_and_still_stops_at_new_bound(
    db, tmp_path, monkeypatch
):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "blocked")
    try:
        request, _ = accepted(worker, pid, cid)
        claimed = worker.state.claim()
        assert claimed
        worker.spawn(claimed)
        handle = worker.child
        assert handle and worker.profile

        def entered():
            worker.messages(handle)
            return (directory / "attempt-1").exists()

        wait_for(entered)
        max_calls = worker.profile["limits"]["max_calls"]
        now = handle.started + max_calls * 125 + 61
        monkeypatch.setattr(analysis_worker, "time", SimpleNamespace(monotonic=lambda: now))
        worker.once()
        assert handle.process.is_alive() and handle.code is None
        assert worker.admission.request(pid, request)["state"] == "running"
        with pytest.raises(CollectionError):
            store.add(pid, "blocked.md", b"Old deadline does not release the project")

        now = handle.started + max_calls * (125 + 60 + 240) + 61
        worker.once()
        assert not handle.process.is_alive()
        worker.once()
        result = worker.admission.request(pid, request)
        assert result["state"] == "failed"
        assert result["error_code"] == "analysis_deadline_exceeded"
        assert result["call_seq"] == 1 and result["result_run_id"] is None
        assert Experiences(db).listing(pid) == []
        assert len(list(directory.glob("attempt-*"))) == 1
        store.add(pid, "released.md", b"New deadline still waits for child termination")
    finally:
        stop(worker, owner)


@pytest.mark.parametrize(
    "mode,expected,calls",
    [
        ("503once", "completed", None),
        ("503always", "failed", 2),
        ("network", "failed", 1),
        ("crash", "failed", 1),
    ],
)
def test_narrow_retry_and_no_blind_resend_or_graph_restart(db, tmp_path, mode, expected, calls):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, mode)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        result = terminal(worker, pid, request)
        assert result["state"] == expected, result
        ledger = db.all("SELECT * FROM analysis_calls ORDER BY seq")
        assert result["call_seq"] == len(ledger) == len(list(directory.glob("attempt-*")))
        if calls is not None:
            assert len(ledger) == calls
            if mode == "503always":
                partial = Experiences(db).detail(pid, str(result["result_run_id"]))["result"]
                assert partial["coverage"]["analysis_status"] == "partial"
                assert partial["coverage"]["unresolved"]
                assert partial["candidates"] == []
            else:
                assert Experiences(db).listing(pid) == []
        else:
            run = Experiences(db).detail(pid, str(result["result_run_id"]))
            assert len(ledger) == run["result"]["model_calls"] + 1
            assert ledger[0]["payload_digest"] == ledger[1]["payload_digest"]
        store.add(pid, "released.md", b"Final failure or completion unlocks")
    finally:
        stop(worker, owner)


def test_cancel_during_retry_wait_prevents_second_entry(db, tmp_path, monkeypatch):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "503always")
    entered, release = threading.Event(), threading.Event()
    original = worker.state.phase

    def pause(*args):
        original(*args)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(worker.state, "phase", pause)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        assert entered.wait(10)
        worker.state.cancel(pid, request)
        release.set()
        result = terminal(worker, pid, request)
        assert result["state"] == "cancelled" and result["call_seq"] == 1
        assert len(list(directory.glob("attempt-*"))) == 1
    finally:
        release.set()
        stop(worker, owner)


def test_cancel_commits_before_call_gate_blocks_model_entry(db, tmp_path, monkeypatch):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = worker.state.enter_call

    def pause(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(worker.state, "enter_call", pause)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        assert entered.wait(10)
        worker.state.cancel(pid, request)
        release.set()
        result = terminal(worker, pid, request)
        assert result["state"] == "cancelled" and result["call_seq"] == 0
        assert not list(directory.glob("attempt-*"))
    finally:
        release.set()
        stop(worker, owner)


@pytest.mark.parametrize("winner", ["cancel", "complete"])
def test_cancel_and_result_commit_race_keeps_first_durable_state(db, tmp_path, monkeypatch, winner):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_finish, original_insert = worker.state.finish, Experiences.insert
    if winner == "cancel":

        def pause(*args, **kwargs):
            if kwargs.get("result") is not None:
                entered.set()
                assert release.wait(5)
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(worker.state, "finish", pause)
    else:

        def pause_insert(self, *args, **kwargs):
            entered.set()  # Project/request locks held in the result transaction
            assert release.wait(5)
            return original_insert(self, *args, **kwargs)

        monkeypatch.setattr(Experiences, "insert", pause_insert)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        assert entered.wait(10)
        if winner == "cancel":
            worker.state.cancel(pid, request)
            release.set()
        else:
            with ThreadPoolExecutor(1) as pool:
                cancel = pool.submit(worker.state.cancel, pid, request)
                release.set()
                assert cancel.result(timeout=10)["state"] == "completed"
        result = terminal(worker, pid, request)
        assert result["state"] == ("cancelled" if winner == "cancel" else "completed")
        assert len(Experiences(db).listing(pid)) == (0 if winner == "cancel" else 1)
    finally:
        release.set()
        stop(worker, owner)


def test_normal_server_close_fences_kills_and_fails_without_resume(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "blocked")
    request, _ = accepted(worker, pid, cid)
    worker.start()
    wait_for(lambda: (directory / "attempt-1").exists())
    handle = worker.child
    stop(worker, owner)
    assert handle and not handle.process.is_alive()
    row = worker.admission.request(pid, request)
    assert row["state"] == "failed" and row["error_code"] == "failed_server_stopped"
    call = db.one("SELECT * FROM analysis_calls WHERE request_id=%s", (request,))
    assert call["outcome"] == "failed" and call["finished_at"] is not None
    assert call["error_code"] == "analysis_call_outcome_unknown"
    assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False
    store.add(pid, "released.md", b"Stopped server restriction released")
    new_owner = AppOwnership(db)
    newer = AnalysisWorker(
        db, store, new_owner, profile=worker.profile, model_factory=offline_factory
    )
    try:
        newer.recover()
        newer.state.open()
        assert newer.once() is False
        assert newer.admission.request(pid, request) == row
        assert len(list(directory.glob("attempt-*"))) == 1
    finally:
        stop(newer, new_owner)


def test_generation_and_owner_fence_late_call_and_success_writes(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    try:
        request, _ = accepted(worker, pid, cid)
        claim = worker.state.claim()
        assert claim is not None
        token = str(claim["owner_token"])
        assert worker.state.claim() is None
        with pytest.raises(CollectionError):
            worker.state.enter_call(pid, request, str(uuid4()), 1, "discover", "a" * 64)
        newer = ExecutionState(db, owner, str(uuid4()), worker.profile)
        newer.fence()
        for operation in (
            lambda: worker.state.enter_call(pid, request, token, 1, "discover", "a" * 64),
            lambda: worker.state.finish(pid, request, token, code="late"),
        ):
            with pytest.raises(CollectionError):
                operation()
        assert db.all("SELECT * FROM analysis_calls") == []
        newer.recover(code="failed_server_interrupted")
        assert worker.admission.request(pid, request)["state"] == "failed"
    finally:
        stop(worker, owner)


def test_actual_attempt_budget_and_corrupt_input_fail_without_success(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    try:
        request, _ = accepted(worker, pid, cid)
        claimed = worker.state.claim()
        assert claimed
        worker.state.child_ready(pid, request, str(claimed["owner_token"]), [0, 0])
        db.execute("UPDATE analysis_requests SET call_seq=512 WHERE id=%s", (request,))
        with pytest.raises(CollectionError) as exc:
            worker.state.enter_call(
                pid,
                request,
                str(claimed["owner_token"]),
                513,
                "discover",
                "a" * 64,
                unit_request_digest="b" * 64,
            )
        assert exc.value.code == "analysis_budget_exceeded"
        worker.state.finish(pid, request, str(claimed["owner_token"]), code=exc.value.code)
        next_request, _ = accepted(worker, pid, cid)
        db.execute(
            "UPDATE analysis_requests SET "
            "input_payload=jsonb_set(input_payload,'{entries,0,text}','\"corrupt\"') WHERE id=%s",
            (next_request,),
        )
        worker.start()
        failed = terminal(worker, pid, next_request)
        assert failed["state"] == "failed" and failed["error_code"] == "analysis_input_corrupt"
        assert not list(directory.glob("attempt-*")) and Experiences(db).listing(pid) == []
    finally:
        stop(worker, owner)


def test_result_rollback_retry_and_lost_commit_response_do_not_duplicate(db, tmp_path, monkeypatch):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    original_insert, original_finish = Experiences.insert, worker.state.finish
    inserts, finishes = [], []

    def rollback_once(self, *args, **kwargs):
        inserts.append(True)
        if len(inserts) == 1:
            raise psycopg.errors.DeadlockDetected("synthetic rollback")
        return original_insert(self, *args, **kwargs)

    def commit_response_lost(*args, **kwargs):
        value = original_finish(*args, **kwargs)
        if kwargs.get("result") is not None and not finishes:
            finishes.append(True)
            raise psycopg.OperationalError("synthetic lost commit response")
        return value

    monkeypatch.setattr(Experiences, "insert", rollback_once)
    monkeypatch.setattr(worker.state, "finish", commit_response_lost)
    try:
        request, body = accepted(worker, pid, cid)
        worker.start()
        result = terminal(worker, pid, request)
        assert result["state"] == "completed"
        wait_for(lambda: worker.child is None)
        assert len(Experiences(db).listing(pid)) == 1 and len(inserts) == 2
        assert worker.admission.request(pid, request)["result_run_id"] == result["result_run_id"]
    finally:
        stop(worker, owner)


def test_collection_worker_completes_and_cleans_failure_while_model_blocked(db, git, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "blocked")
    collector = Worker(db, owner)
    try:
        root = tmp_path / "repo"
        (root / "counter.py").write_text("counter = 0\n")
        git("add", ".")
        git("commit", "-m", "Synthetic collection during AI")
        registered = Projects(db).register(
            Registration(path=str(root), name="other", status="ongoing", base_branch="main")
        )
        collected = str(registered["collections"][0]["id"])
        assert collector.once() and db.detail(collected)["state"] == "completed"
        pid, cid = str(registered["id"]), collected
        store.add(pid, "context.md", b"Same project synthetic counter evidence")
        request, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(lambda: (directory / "attempt-1").exists())
        collected = str(Projects(db).collect_now(pid)["id"])
        assert collector.once() and db.detail(collected)["state"] == "completed"
        failed = str(Projects(db).collect_now(str(registered["id"]))["id"])
        db.mark_cleanup(failed)
        assert collector.once() and not db.all("SELECT id FROM collections WHERE id=%s", (failed,))
        assert worker.admission.request(pid, request)["state"] == "running"
        worker.state.cancel(pid, request)
        assert terminal(worker, pid, request)["state"] == "cancelled"
    finally:
        collector.close()
        stop(worker, owner)


def test_duplicate_app_refused_before_migration_or_recovery(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    owner = AppOwnership(db)

    def forbidden():
        pytest.fail("Duplicate app must be rejected before migration")

    monkeypatch.setattr(db, "migrate", forbidden)
    try:
        with pytest.raises(RuntimeError, match="already using"):
            with TestClient(create_app(db)):
                pass
    finally:
        owner.close()


def test_parent_process_death_watchdog_quiescence_restart_failure_no_resume(db, tmp_path):
    pid, cid, store, initial_owner, initial, directory = setup_worker(db, tmp_path, "blocked")
    stop(initial, initial_owner)
    script = """
import json, sys, time
from pathlib import Path
from uuid import UUID, uuid4
from analysis_worker_fixtures import offline_factory
from project_log.analysis_admission import PrepareAnalysis, SubmitAnalysis
from project_log.analysis_worker import AnalysisWorker
from project_log.db import Database
from project_log.materials import Materials
from project_log.worker import AppOwnership
db = Database(sys.argv[1])
owner = AppOwnership(db)
worker = AnalysisWorker(db, Materials(db, Path(sys.argv[2])), owner,
    profile=json.loads(sys.argv[5]), model_factory=offline_factory)
worker.recover()
worker.state.open()
p = worker.admission.prepare(sys.argv[3], PrepareAnalysis(
    preparation_id=uuid4(), end_collection_id=UUID(sys.argv[4])))
r = worker.admission.submit(sys.argv[3], SubmitAnalysis(request_id=uuid4(),
    preparation_id=p['id'], input_digest=p['input_digest'],
    consent_digest=p['consent_digest'],
    relation_recovery_digest=p['descriptor'].get('relation_recovery_digest'), confirmed=True))
Path(sys.argv[6]).write_text(str(r['id']))
worker.start()
while True:
    time.sleep(0.1)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in ("src", "tests", "."))
    record = directory / "request.txt"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            db.dsn,
            str(store.root),
            pid,
            cid,
            json.dumps(initial.profile),
            str(record),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for(lambda: (directory / "attempt-1").exists())
        request = record.read_text()
        token = str(
            db.one("SELECT owner_token FROM analysis_requests WHERE id=%s", (request,))[
                "owner_token"
            ]
        )
        assert initial.files.quiet(token) is False
        process.kill()
        process.wait(timeout=5)
        owner = AppOwnership(db)
        newer = AnalysisWorker(
            db, store, owner, profile=initial.profile, model_factory=offline_factory
        )
        try:
            newer.recover()  # fences and waits for real orphan execution lock release
            assert newer.files.quiet(token)
            row = newer.admission.request(pid, request)
            assert row["state"] == "failed" and row["error_code"] == "failed_server_interrupted"
            assert row["call_seq"] == 1 and len(list(directory.glob("attempt-*"))) == 1
            call = db.one("SELECT * FROM analysis_calls WHERE request_id=%s", (request,))
            assert call["outcome"] == "failed" and call["finished_at"] is not None
            assert call["error_code"] == "analysis_call_outcome_unknown"
            newer.state.open()
            assert newer.once() is False and Experiences(db).listing(pid) == []
            store.add(pid, "released.md", b"Restart cleanup unlocks old project")
        finally:
            stop(newer, owner)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()


def test_recovery_does_not_unlock_when_previous_local_execution_not_quiet(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    request, _ = accepted(worker, pid, cid)
    claimed = worker.state.claim()
    assert claimed
    fd = worker.files.lock(str(claimed["owner_token"]), create=True)
    owner.close()  # only the application lock is gone; local execution lock is still held
    new_owner = AppOwnership(db)
    newer = AnalysisWorker(
        db, store, new_owner, profile=worker.profile, model_factory=offline_factory
    )
    try:
        with pytest.raises(CollectionError) as exc:
            newer.recover()
        assert exc.value.code == "analysis_worker_not_quiet"
        assert newer.admission.request(pid, request)["state"] == "running"
        assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False
        with pytest.raises(CollectionError):
            store.add(pid, "blocked.md", b"Cannot prove old termination")
        os.close(fd)
        fd = -1
        newer.recover()
        assert newer.admission.request(pid, request)["state"] == "failed"
    finally:
        if fd >= 0:
            os.close(fd)
        stop(newer, new_owner)


def test_unexpected_save_failure_rolls_back_result_and_releases_after_failure(
    db, tmp_path, monkeypatch
):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    original = Experiences.insert

    def reject_after_insert(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise psycopg.errors.CheckViolation("synthetic save rollback")

    monkeypatch.setattr(Experiences, "insert", reject_after_insert)
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        result = terminal(worker, pid, request)
        assert result["state"] == "failed"
        assert (
            Experiences(db).listing(pid) == [] and db.all("SELECT * FROM experience_evidence") == []
        )
        from project_log.analysis_events import validated_candidates

        with db.connect() as conn:
            row = conn.execute("SELECT * FROM analysis_requests WHERE id=%s", (request,)).fetchone()
            assert row and len(validated_candidates(conn, row)) == 1
        diagnostics = db.all(
            "SELECT data FROM analysis_events WHERE request_id=%s AND kind='finalization_failed'",
            (request,),
        )
        assert any(e["data"].get("operation") == "partial_result_persist" for e in diagnostics)
        store.add(pid, "released.md", b"Known final failure unlocks")
    finally:
        stop(worker, owner)


def test_fastapi_lifecycle_owns_runtime_but_default_execution_remains_disabled(db):
    from fastapi.testclient import TestClient

    with TestClient(create_app(db)) as client:
        assert client.get("/api/health").json()["worker_running"] is True
        assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False
        assert client.app.state.analysis_worker.thread.is_alive()
    assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False


def test_sdk_mock_retry_uses_fresh_wire_gate_and_preserves_failure_evidence(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "sdk503")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        result = terminal(worker, pid, request)
        assert result["state"] == "completed", result
        ledger = db.all("SELECT * FROM analysis_calls ORDER BY seq")
        assert ledger[0]["outcome"] == "failed"
        assert ledger[0]["error_code"] == "provider_temporarily_unavailable"
        assert ledger[1]["outcome"] == "responded"
        assert ledger[0]["payload_digest"] == ledger[1]["payload_digest"]
        cooldown = ledger[1]["entered_at"] - ledger[0]["finished_at"]
        assert cooldown.total_seconds() >= worker.profile["retry_delay"] == 30.0
        run = Experiences(db).detail(pid, str(result["result_run_id"]))
        assert len(ledger) == run["result"]["model_calls"] + 1
        retries = db.all(
            "SELECT data FROM analysis_events WHERE request_id=%s "
            "AND kind='attempt_retry_scheduled'",
            (request,),
        )
        assert retries and retries[0]["data"]["delay_seconds"] == 30.0
        assert retries[0]["data"]["code"] == "provider_temporarily_unavailable"
        assert retries[0]["data"]["reason"] == "reviewed_transport_retry_within_shared_budget"
    finally:
        stop(worker, owner)


def test_db_outage_stops_child_then_recovers_failure_without_resuming(db, tmp_path, monkeypatch):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "blocked")
    original = db.connect
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(lambda: (directory / "attempt-1").exists())
        handle = worker.child
        assert handle is not None

        def unavailable():
            raise psycopg.OperationalError("Synthetic temporary DB outage")

        with monkeypatch.context() as patch:
            patch.setattr(db, "connect", unavailable)
            wait_for(lambda: not handle.process.is_alive())
            with original() as conn:
                assert (
                    conn.execute(
                        "SELECT state FROM analysis_requests WHERE id=%s", (request,)
                    ).fetchone()["state"]
                    == "running"
                )
        result = terminal(worker, pid, request)
        assert result["state"] == "failed" and result["error_code"] == "analysis_runtime_error"
        assert len(list(directory.glob("attempt-*"))) == 1
        assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False
        store.add(pid, "released.md", b"Failure persisted after DB returns")
    finally:
        stop(worker, owner)


def test_known_child_crash_is_not_overwritten_by_later_cancel_intent(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "crash")
    try:
        request, _ = accepted(worker, pid, cid)
        claimed = worker.state.claim()
        assert claimed
        worker.spawn(claimed)
        handle = worker.child
        assert handle

        def crashed():
            worker.messages(handle)
            return handle.process.exitcode == 19

        wait_for(crashed)
        worker.state.cancel(pid, request)
        worker.once()
        result = worker.admission.request(pid, request)
        assert result["state"] == "failed" and result["error_code"] == "analysis_child_failed"
        assert result["cancel_requested_at"] is not None
    finally:
        stop(worker, owner)


def test_zero_candidates_is_complete_result_not_partial_or_failure(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, "empty")
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        row = terminal(worker, pid, request)
        assert row["state"] == "completed" and row["call_seq"] == 1
        run = Experiences(db).detail(pid, str(row["result_run_id"]))
        assert run["result"]["candidates"] == [] and run["result"]["coverage"]["input_entries"] == 1
    finally:
        stop(worker, owner)


def test_missing_ready_child_lock_identity_does_not_prove_termination(db, tmp_path):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path)
    fd = -1
    try:
        request, _ = accepted(worker, pid, cid)
        claimed = worker.state.claim()
        assert claimed
        token = str(claimed["owner_token"])
        fd = worker.files.lock(token, create=True)
        info = os.fstat(fd)
        worker.state.child_ready(pid, request, token, [info.st_dev, info.st_ino])
        worker.files.path(token, ".lock").unlink()  # only this test's synthetic inode
        owner.close()
        new_owner = AppOwnership(db)
        newer = AnalysisWorker(
            db, store, new_owner, profile=worker.profile, model_factory=offline_factory
        )
        try:
            with pytest.raises(CollectionError) as exc:
                newer.recover()
            assert exc.value.code == "analysis_worker_identity_unknown"
            assert newer.admission.request(pid, request)["state"] == "running"
            assert db.one("SELECT ready FROM analysis_runtime")["ready"] is False
        finally:
            stop(newer, new_owner)
    finally:
        if fd >= 0:
            os.close(fd)
        owner.close()
