"""Real isolated PostgreSQL and spawned synthetic workers; no live data or Gemini."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_analysis_admission import collection, submission
from test_analysis_worker import accepted, setup_worker, stop, terminal, wait_for

from project_log.analysis_admission import PrepareAnalysis
from project_log.analysis_checkpoints import read_rows
from project_log.analysis_input import canonical
from project_log.analysis_snapshot import decode_snapshot
from project_log.analysis_worker import AnalysisWorker
from project_log.api import create_app
from project_log.experience_store import Experiences
from project_log.experiences import ExperienceGraph
from project_log.policy import sha256
from project_log.project_deletion import ProjectDeletion


def prepare_resume(worker, pid, cid, request):
    return worker.admission.prepare(
        pid,
        PrepareAnalysis(
            preparation_id=uuid4(),
            end_collection_id=UUID(cid),
            resume_from_request_id=UUID(request),
        ),
    )


def original(db, request):
    return db.one("SELECT * FROM analysis_requests WHERE id=%s", (request,))


@pytest.mark.parametrize("stage", ["consolidate", "reconcile", "reconstruct", "audit", "assess"])
def test_spawned_recovery_preserves_prefix_and_provenance_and_final_atomicity(
    db, tmp_path, monkeypatch, stage
):
    from test_experiences import OfflineModel

    control = tmp_path / "mode"
    control.write_text("network_stage_" + stage)
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, control_file=control)
    try:
        store.add(pid, "second.md", b"Another counter correction")
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        assert failed["state"] == "failed"
        saved = read_rows(db, pid, rid)
        assert saved and all(c["task"] != stage for c in saved)
        source = original(db, rid)
        assert not Experiences(db).listing(pid)
        # A later Collection is excluded. Resume must never regenerate the snapshot.
        later = collection(db, pid)

        def forbidden(*args, **kwargs):
            pytest.fail("Resume must not read/reprepare source bodies")

        monkeypatch.setattr("project_log.experience_input.ExperienceInputs.prepare", forbidden)
        prep = prepare_resume(worker, pid, cid, rid)
        if stage == "reconcile":
            # The durable relation budget must not resend an unknown transmission.
            assert prep["state"] == "failed"
            assert prep["error_code"] == "analysis_relation_budget_unknown"
            assert original(db, rid) == source
            assert read_rows(db, pid, rid) == saved
            assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == failed["call_seq"]
            assert not Experiences(db).listing(pid)
            return
        assert prep["state"] == "ready", prep
        assert prep["input_digest"] == source["input_digest"]
        assert (
            worker.admission.transfer(pid, str(prep["id"]), download=True)["input_digest"]
            == source["input_digest"]
        )
        assert prep["consent_digest"] != source["consent"]["consent_digest"]
        assert later not in prep["descriptor"]["collection_ids"]
        body = submission(prep)
        control.write_text("success")
        resumed = worker.admission.submit(pid, body)
        done = terminal(worker, pid, str(resumed["id"]))
        assert done["state"] == "completed", done
        current = original(db, str(done["id"]))
        assert current["input_payload"] == source["input_payload"]
        assert current["inherited_attempts"] == failed["call_seq"]
        all_cp = read_rows(db, pid, str(done["id"]))
        copied = all_cp[: len(saved)]
        assert [{k: v for k, v in c.items() if k != "request_id"} for c in copied] == [
            {k: v for k, v in c.items() if k != "request_id"} for c in saved
        ]
        assert done["call_seq"] == len(all_cp) - len(saved)
        assert worker.admission.submit(pid, body)["result_run_id"] == done["result_run_id"]
        result = Experiences(db).detail(pid, str(done["result_run_id"]))
        snapshot = decode_snapshot(
            source["input_payload"], source["input_manifest"], source["input_digest"]
        )
        assert result["result"] == ExperienceGraph(OfflineModel()).run(snapshot).model_dump()
        assert original(db, rid) == source
        assert read_rows(db, pid, rid) == saved
        assert len(Experiences(db).listing(pid)) == 1
        assert not list(worker.files.root.glob("*.json"))
        store.add(pid, "released.md", b"Unlocked after completion")
    finally:
        stop(worker, owner)


def test_duplicate_resume_and_old_consent_are_rejected_without_extra_dispatch(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        assert terminal(worker, pid, rid)["state"] == "failed"
        prep1, prep2 = (prepare_resume(worker, pid, cid, rid) for _ in range(2))
        old = original(db, rid)["consent"]["consent_digest"]
        rejected = worker.admission.submit(pid, submission(prep1, consent_digest=old))
        assert rejected["error_code"] == "analysis_consent_mismatch"
        # Stop dispatch while racing distinct request IDs, retaining ready admission.
        worker.stop_event.set()
        worker.thread.join(timeout=10)
        worker.state.open()
        bodies = [submission(prep1), submission(prep2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda body: worker.admission.submit(pid, body), bodies))
        assert sorted(r["state"] for r in rows) == ["queued", "rejected"]
        assert (
            len(
                db.all(
                    "SELECT id FROM analysis_requests WHERE resume_from_request_id=%s "
                    "AND accepted_at IS NOT NULL",
                    (rid,),
                )
            )
            == 1
        )
        assert all(r["call_seq"] == 0 for r in rows)
    finally:
        stop(worker, owner)


@pytest.mark.parametrize("change", ["input", "profile", "cancelled", "missing", "gap", "digest"])
def test_invalid_resume_contract_fails_before_model_dispatch(db, tmp_path, change):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        terminal(worker, pid, rid)
        count = db.one("SELECT count(*) AS n FROM analysis_calls")["n"]
        if change == "input":
            store.add(pid, "new.md", b"Changed source set")
        elif change == "profile":
            worker.admission.profile["model"] = "different-model"
        elif change == "cancelled":
            db.execute("UPDATE analysis_requests SET state='cancelled' WHERE id=%s", (rid,))
        elif change == "missing":
            db.execute("DELETE FROM analysis_checkpoints WHERE request_id=%s", (rid,))
        elif change == "gap":
            db.execute("DELETE FROM analysis_checkpoints WHERE request_id=%s AND ordinal=1", (rid,))
        else:
            db.execute(
                "UPDATE analysis_checkpoints SET response_digest=%s "
                "WHERE request_id=%s AND ordinal=1",
                ("0" * 64, rid),
            )
        prep = prepare_resume(worker, pid, cid, rid)
        assert prep["state"] == "failed", prep
        assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == count
        assert not Experiences(db).listing(pid)
    finally:
        stop(worker, owner)


def test_discovery_failure_resumes_after_last_complete_batch(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("discovery_failure_after_one")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        store.add(pid, "large.md", b"counter " * 15000)
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        assert failed["state"] == "failed" and failed["call_seq"] == 2
        assert len(read_rows(db, pid, rid)) == 1
        control.write_text("discovery_empty")
        prep = prepare_resume(worker, pid, cid, rid)
        row = worker.admission.submit(pid, submission(prep))
        done = terminal(worker, pid, str(row["id"]))
        assert done["state"] == "completed", done
        checkpoints = read_rows(db, pid, str(row["id"]))
        assert done["call_seq"] == len(checkpoints) - 1
        assert done["inherited_attempts"] == 2
        assert not Experiences(db).detail(pid, str(done["result_run_id"]))["result"]["candidates"]
    finally:
        stop(worker, owner)


@pytest.mark.parametrize(
    "fault", ["before_commit", "rollback", "truncated_file", "lost_ack", "last_ack"]
)
def test_checkpoint_save_faults_stop_dispatch_and_preserve_only_committed_prefix(
    db, tmp_path, monkeypatch, fault
):
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path)
    try:
        persist = worker.state.checkpoint
        if fault == "rollback":
            import psycopg

            execute = psycopg.Connection.execute

            def fail_insert(conn, query, *args, **kwargs):
                value = execute(conn, query, *args, **kwargs)
                if str(query).startswith("INSERT INTO analysis_checkpoints"):
                    raise psycopg.errors.SerializationFailure("Synthetic rollback after INSERT")
                return value

            monkeypatch.setattr(psycopg.Connection, "execute", fail_insert)
        if fault == "truncated_file":
            read = worker.files.read

            def truncated(*args, **kwargs):
                raw = read(*args, **kwargs)
                return raw[:-1] if kwargs.get("suffix") == ".checkpoint.json" else raw

            monkeypatch.setattr(worker.files, "read", truncated)

        def injected(project, request, token, payload):
            if fault == "before_commit":
                raise OSError("Synthetic checkpoint storage interruption")
            persist(project, request, token, payload)
            if fault == "lost_ack" or payload["task"] == "assess":
                raise OSError("Synthetic lost checkpoint acknowledgment")

        monkeypatch.setattr(worker.state, "checkpoint", injected)
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        wait_for(lambda: not worker.thread.is_alive())
        saved = read_rows(db, pid, rid)
        assert not Experiences(db).listing(pid)
        if fault in {"before_commit", "rollback", "truncated_file"}:
            assert not saved and failed["call_seq"] == 1
            return
        assert saved
        if fault == "last_ack":
            assert saved[-1]["task"] == "assess"
            # A fully checkpointed Graph can finish with ZERO new API attempts,
            # even when the inherited attempt budget has no remaining capacity.
            db.execute(
                "UPDATE analysis_requests SET inherited_attempts=%s WHERE id=%s",
                (512 - failed["call_seq"], rid),
            )
        worker.close()
        worker = AnalysisWorker(
            db, store, owner, profile=worker.profile, model_factory=worker.factory
        )
        worker.recover()
        worker.state.open()
        prep = prepare_resume(worker, pid, cid, rid)
        row = worker.admission.submit(pid, submission(prep))
        worker.start()
        done = terminal(worker, pid, str(row["id"]))
        assert done["state"] == "completed", done
        assert done["call_seq"] == len(read_rows(db, pid, str(row["id"]))) - len(saved)
        if fault == "last_ack":
            assert done["call_seq"] == 0 and done["inherited_attempts"] == 512
    finally:
        stop(worker, owner)


def test_per_execution_budget_allows_cumulative_512_without_charging_replay(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        # A boundary fixture models an ancestor having spent the other attempts.
        db.execute(
            "UPDATE analysis_requests SET inherited_attempts=%s WHERE id=%s",
            (511 - failed["call_seq"], rid),
        )
        control.write_text("success")
        prep = prepare_resume(worker, pid, cid, rid)
        row = worker.admission.submit(pid, submission(prep))
        done = terminal(worker, pid, str(row["id"]))
        assert done["state"] == "completed" and done["inherited_attempts"] == 511
        assert done["call_seq"] == 2  # audit + assess, not the replayed prefix
        assert done["inherited_attempts"] + done["call_seq"] == 513
        assert len(Experiences(db).listing(pid)) == 1
    finally:
        stop(worker, owner)


def test_cancel_after_checkpoint_retains_prefix_but_cannot_resume(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("blocked_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(lambda: original(db, rid)["call_seq"] == 3)
        assert len(read_rows(db, pid, rid)) == 2
        worker.state.cancel(pid, rid)
        cancelled = terminal(worker, pid, rid)
        assert cancelled["state"] == "cancelled"
        assert len(read_rows(db, pid, rid)) == 2
        assert prepare_resume(worker, pid, cid, rid)["error_code"] == "analysis_resume_unavailable"
        store.add(pid, "released.md", b"Unlocked after child terminated")
    finally:
        stop(worker, owner)


def test_server_shutdown_retains_checkpoint_and_requires_manual_new_consent(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        terminal(worker, pid, rid)
        prep = prepare_resume(worker, pid, cid, rid)
        # Queue without dispatch, then let normal shutdown classify it as failed.
        worker.stop_event.set()
        worker.thread.join(timeout=10)
        worker.state.open()
        resumed = worker.admission.submit(pid, submission(prep))
        worker.close()
        row = original(db, str(resumed["id"]))
        assert row["state"] == "failed" and row["error_code"] == "failed_server_stopped"
        assert row["call_seq"] == 0 and read_rows(db, pid, str(row["id"]))
        worker.recover()
        worker.state.open()
        assert not db.all("SELECT id FROM analysis_requests WHERE state IN ('queued','running')")
        fresh = prepare_resume(worker, pid, cid, str(row["id"]))
        assert fresh["state"] == "ready"
        assert fresh["consent_digest"] != prep["consent_digest"]
    finally:
        stop(worker, owner)


@pytest.mark.parametrize(
    "mode", ["checkpoint_sdk_failure", "checkpoint_sdk503", "checkpoint_sdk500"]
)
def test_synthetic_sdk_quota_and_503_attempts_exclude_checkpoint_replay(db, tmp_path, mode):
    control = tmp_path / "mode"
    control.write_text(mode)
    pid, cid, store, owner, worker, directory = setup_worker(
        db, tmp_path, mode="sdk_checkpoint", control_file=control
    )
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        assert failed["state"] == "failed", failed
        saved = read_rows(db, pid, rid)
        assert len(saved) == 2
        attempts = 3 if mode == "checkpoint_sdk_failure" else 4
        assert failed["call_seq"] == attempts
        if mode == "checkpoint_sdk500":
            assert failed["error_code"] == "provider_server_error_h500"
            ledger = db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (rid,))
            assert [c["outcome"] for c in ledger] == ["responded", "responded", "failed", "failed"]
            assert (ledger[-1]["entered_at"] - ledger[-2]["finished_at"]).total_seconds() >= 30
        assert json.loads((directory / "quota.json").read_text())["count"] == attempts
        control.write_text("checkpoint_sdk_success")
        prep = prepare_resume(worker, pid, cid, rid)
        row = worker.admission.submit(pid, submission(prep))
        done = terminal(worker, pid, str(row["id"]))
        assert done["state"] == "completed", done
        assert done["call_seq"] == 2  # audit + assess, no discover/reconstruct re-entry
        assert done["inherited_attempts"] == attempts
        assert json.loads((directory / "quota.json").read_text())["count"] == attempts + 2
    finally:
        stop(worker, owner)


@pytest.mark.parametrize("kind", ["request_digest", "semantic_reference"])
def test_replayed_corruption_aborts_without_a_fresh_api_attempt(db, tmp_path, kind):
    from psycopg.types.json import Jsonb

    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        terminal(worker, pid, rid)
        if kind == "request_digest":
            db.execute(
                "UPDATE analysis_checkpoints SET request_digest=%s "
                "WHERE request_id=%s AND ordinal=1",
                ("0" * 64, rid),
            )
        else:
            response = read_rows(db, pid, rid)[0]["response"]
            response["seeds"][0]["evidence_ids"] = ["unprovided-source"]
            db.execute(
                "UPDATE analysis_checkpoints SET response=%s,response_digest=%s "
                "WHERE request_id=%s AND ordinal=1",
                (Jsonb(response), sha256(canonical(response).encode()), rid),
            )
        prep = prepare_resume(worker, pid, cid, rid)
        row = worker.admission.submit(pid, submission(prep))
        result = terminal(worker, pid, str(row["id"]))
        assert result["state"] == "failed" and result["call_seq"] == 0
        assert not Experiences(db).listing(pid)
    finally:
        stop(worker, owner)


def test_project_deletion_removes_checkpoint_chain_and_final_results(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        terminal(worker, pid, rid)
        prep = prepare_resume(worker, pid, cid, rid)
        control.write_text("success")
        row = worker.admission.submit(pid, submission(prep))
        assert terminal(worker, pid, str(row["id"]))["state"] == "completed"
        assert read_rows(db, pid, rid) and read_rows(db, pid, str(row["id"]))
        name = db.one("SELECT name FROM projects WHERE id=%s", (pid,))["name"]
        ProjectDeletion(db, store).delete(pid, name)
        assert not db.all("SELECT id FROM analysis_requests WHERE project_id=%s", (pid,))
        assert not db.all("SELECT * FROM analysis_checkpoints WHERE project_id=%s", (pid,))
        assert not db.all("SELECT * FROM analysis_calls WHERE project_id=%s", (pid,))
    finally:
        stop(worker, owner)


def test_input_change_after_resume_preview_is_rejected_without_copy_or_dispatch(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        terminal(worker, pid, rid)
        prep = prepare_resume(worker, pid, cid, rid)
        before = read_rows(db, pid, rid)
        store.add(pid, "changed.md", b"Added after preview")
        row = worker.admission.submit(pid, submission(prep))
        assert row["state"] == "rejected" and row["error_code"] == "analysis_input_changed"
        assert not read_rows(db, pid, str(row["id"])) and row["call_seq"] == 0
        assert read_rows(db, pid, rid) == before
        assert original(db, str(row["id"]))["input_payload"] is None
    finally:
        stop(worker, owner)


def test_http_resume_preview_consent_idempotency_and_project_isolation(db, tmp_path, monkeypatch):
    from test_analysis_api import body, prepare, setup, submit
    from test_analysis_api import terminal as http_terminal
    from test_materials import project

    pid, cid, directory, mode, options = setup(db, tmp_path)
    other = project(db, tmp_path, "other-project")
    with TestClient(create_app(db, **options)) as client:
        first = http_terminal(
            client, pid, submit(client, pid, body(prepare(client, pid, cid)))["id"]
        )
        previous = client.get(f"/api/projects/{pid}/experiences/{first['result_run_id']}").json()
        mode.write_text("network_stage_audit")
        failed = http_terminal(
            client, pid, submit(client, pid, body(prepare(client, pid, cid)))["id"]
        )
        path = f"/api/projects/{pid}/analysis"
        before = original(db, failed["id"])
        data = {
            "preparation_id": str(uuid4()),
            "end_collection_id": cid,
            "resume_from_request_id": failed["id"],
        }
        wrong = client.post(
            f"/api/projects/{other}/analysis/preparations",
            json={**data, "preparation_id": str(uuid4())},
        ).json()
        assert wrong["state"] == "failed"

        def forbidden(*args, **kwargs):
            pytest.fail("Resume must use accepted bytes, not reprepare sources")

        monkeypatch.setattr("project_log.experience_input.ExperienceInputs.prepare", forbidden)
        prep = client.post(path + "/preparations", json=data).json()
        assert prep["state"] == "ready", prep
        preview = client.get(path + f"/preparations/{prep['id']}/transfer?download=true")
        assert preview.headers["cache-control"] == "no-store"
        assert preview.json()["input_digest"] == before["input_digest"]
        assert before["input_payload"] == original(db, failed["id"])["input_payload"]
        mode.write_text("success")
        payload = body(prep)
        row = submit(client, pid, payload)
        assert submit(client, pid, payload)["id"] == row["id"]
        done = http_terminal(client, pid, row["id"])
        assert done["state"] == "completed" and done["reused_checkpoints"] == 2
        assert done["call_seq"] == 2 and done["inherited_attempts"] == failed["call_seq"]
        history = client.get(path).json()["executions"]
        assert (
            next(r for r in history if r["id"] == failed["id"])["resumed_by_request_id"]
            == row["id"]
        )
        assert (
            client.get(f"/api/projects/{pid}/experiences/{first['result_run_id']}").json()
            == previous
        )
        assert client.get(f"/api/projects/{other}/analysis/requests/{row['id']}").status_code == 404


def test_010_upgrade_preserves_accepted_failure_and_never_invents_old_checkpoints(tmp_path):
    from psycopg.types.json import Jsonb
    from test_materials import project

    from project_log.analysis_execution import execution_profile
    from project_log.analysis_snapshot import encode_snapshot
    from project_log.db import Database
    from project_log.experience_input import Evidence, ExperienceInput
    from scripts.verification_db import temporary_database

    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL)"
            )
            for path in sorted(Path("src/project_log/migrations").glob("*.sql"))[:10]:
                raw = path.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (%s,%s)",
                    (path.name, hashlib.sha256(raw).hexdigest()),
                )
        pid = project(db, tmp_path)
        cid = collection(db, pid)
        snapshot = ExperienceInput(
            pid,
            "synthetic-preserved-time",
            "[]",
            (Evidence("synthetic", "{}", "Original fixed input"),),
        )
        preparation, request, generation, owner = (str(uuid4()) for _ in range(4))
        # Historical fixture predates the new checkpoint contract, like the three
        # real failures. It does not pretend to have recoverable model responses.
        profile = execution_profile("synthetic-offline")
        profile.pop("checkpoint_policy")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO analysis_preparations(id,project_id,end_collection_id,generation,"
                "state,source_fingerprint,input_digest,consent_digest,descriptor) "
                "VALUES (%s,%s,%s,%s,'consumed','synthetic',%s,'synthetic',%s)",
                (preparation, pid, cid, generation, snapshot.digest, Jsonb({"profile": profile})),
            )
            conn.execute(
                "INSERT INTO analysis_requests(id,project_id,preparation_id,submission_digest,"
                "generation,state,accepted_at,finished_at,consent,input_digest,input_manifest,"
                "input_payload,owner_token,call_seq,error_code) "
                "VALUES (%s,%s,%s,'synthetic',%s,'failed',clock_timestamp(),clock_timestamp(),"
                "%s,%s,%s,%s,%s,1,'provider_network_error')",
                (
                    request,
                    pid,
                    preparation,
                    generation,
                    Jsonb(
                        {
                            "confirmed": True,
                            "input_digest": snapshot.digest,
                            "descriptor": {"profile": profile},
                        }
                    ),
                    snapshot.digest,
                    Jsonb(snapshot.manifest()),
                    Jsonb(encode_snapshot(snapshot)),
                    owner,
                ),
            )
            conn.execute(
                "INSERT INTO analysis_calls(request_id,project_id,generation,owner_token,seq,"
                "phase,payload_digest,outcome) VALUES (%s,%s,%s,%s,1,'discover',%s,'failed')",
                (request, pid, generation, owner, "a" * 64),
            )
        before = {
            table: db.all(f"SELECT * FROM {table} ORDER BY 1")
            for table in (
                "analysis_preparations",
                "analysis_requests",
                "analysis_calls",
                "schema_migrations",
            )
        }
        db.migrate()
        db.migrate()
        for table, rows in before.items():
            after = db.all(f"SELECT * FROM {table} ORDER BY 1")
            if table == "schema_migrations":
                after = after[:10]
            assert [
                {key: row[key] for key in prior} for row, prior in zip(after, rows, strict=True)
            ] == rows
        assert not db.all("SELECT * FROM analysis_checkpoints")
        upgraded = original(db, request)
        assert upgraded["state"] == "failed"
        assert upgraded["inherited_attempts"] == 0 and upgraded["resume_from_request_id"] is None
