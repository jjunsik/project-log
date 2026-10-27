"""Pinned one-shot diagnostics through PostgreSQL/spawn/native SDK, never live Gemini."""

import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from test_analysis_admission import submission
from test_analysis_resume import original, prepare_resume
from test_analysis_worker import accepted, setup_worker, stop, terminal
from test_experience_gemini import ModelConnection
from test_gemini import FakeConnection

from project_log.analysis_admission import PrepareAnalysis
from project_log.analysis_api import request_view
from project_log.analysis_checkpoints import read_rows
from project_log.analysis_diagnostic import POLICY, configured_target, normalize
from project_log.analysis_input import canonical
from project_log.analysis_worker import AnalysisWorker
from project_log.experience_gemini import GeminiExperienceModel
from project_log.experience_store import Experiences
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import FreeTierQuota
from project_log.git import CollectionError
from project_log.policy import sha256
from project_log.worker import AppOwnership


def diagnostic_factory(profile, gate):
    directory = Path(profile["test_directory"])
    mode = Path(profile["test_control_file"]).read_text().strip()

    class Connection(ModelConnection):
        def handle_request(self, request):
            task = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])[
                "task"
            ]
            with (directory / "entries.jsonl").open("a") as log:
                log.write(
                    canonical(
                        {
                            "task": task,
                            "digest": sha256(request.content),
                            "endpoint": str(request.url),
                        }
                    )
                    + "\n"
                )
            if task == "reconcile":
                (directory / "wire-size.json").write_text(
                    json.dumps({"bytes": len(request.content)})
                )
                if mode == "blocked":
                    while not (directory / "release").exists():
                        time.sleep(0.02)
                if mode == "network":
                    raise httpx.ReadTimeout("PRIVATE uncertain transmission")
                if mode in {"400", "503"}:
                    self.status = int(mode)
                    self.payload = json.dumps(
                        {
                            "error": {
                                "code": self.status,
                                "status": "INVALID_ARGUMENT",
                                "message": "A schema has too many states PRIVATE",
                            }
                        }
                    ).encode()
                    return FakeConnection.handle_request(self, request)
            return super().handle_request(request)

    class AlteredInstruction(GeminiExperienceModel):
        def complete(self, task, instruction, chunks, context, schema):
            return super().complete(task, instruction + " changed", chunks, context, schema)

    cls = AlteredInstruction if mode == "wire" else GeminiExperienceModel
    return cls(
        GeminiConfig(profile["model"], "synthetic-test-key", True),
        before_send=gate,
        test_transport_factory=lambda: Connection(retries=0, verify=True, trust_env=False),
        quota=FreeTierQuota(directory / "diagnostic-quota.json", stopped=gate.__self__.stopped),
    )


def diagnostic_setup(db, tmp_path):
    mode = tmp_path / "diagnostic-mode"
    mode.write_text("400")
    pid, cid, store, owner, worker, directory = setup_worker(
        db, tmp_path, "sdk_diagnostic", control_file=mode
    )
    worker.factory = diagnostic_factory
    store.add(pid, "second.md", b"Another synthetic counter correction")
    rid, _ = accepted(worker, pid, cid)
    worker.start()
    failed = terminal(worker, pid, rid)
    assert failed["error_code"] == "provider_invalid_request_schema_complexity"
    before = original(db, rid)
    calls = db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (rid,))
    rows = read_rows(db, pid, rid)
    assert len(rows) == 2 and failed["call_seq"] == 3
    target = normalize(
        dict(
            policy=POLICY,
            project_id=pid,
            request_id=rid,
            input_digest=before["input_digest"],
            original_profile_digest=sha256(
                canonical(before["consent"]["descriptor"]["profile"]).encode()
            ),
            wire_digest=calls[-1]["payload_digest"],
            wire_bytes=0 + len_wire(directory),
            checkpoint_count=2,
            call_seq=3,
            model=worker.profile["model"],
        )
    )
    # Same ownership/generation as a real restart; no mutation of the original failure.
    profile = worker.profile
    stop(worker, owner)
    owner = AppOwnership(db)
    worker = AnalysisWorker(
        db,
        store,
        owner,
        profile=profile,
        model_factory=diagnostic_factory,
        diagnostic_target=target,
    )
    worker.recover()
    worker.state.open()
    return pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, mode


def len_wire(directory):
    # The wire body is deterministic. Record its byte length without writing raw bodies.
    return json.loads((directory / "wire-size.json").read_text())["bytes"]


def prepare_diagnostic(worker, pid, cid, rid):
    return worker.admission.prepare(
        pid,
        PrepareAnalysis(
            preparation_id=uuid4(),
            end_collection_id=UUID(cid),
            resume_from_request_id=UUID(rid),
            diagnostic_retry=True,
        ),
    )


@pytest.mark.parametrize(
    "mode,code",
    [
        ("400", "provider_invalid_request_schema_complexity"),
        ("503", "provider_temporarily_unavailable"),
        ("200", "analysis_diagnostic_response_received"),
        ("network", "provider_read_timeout_transport_hunknown"),
    ],
)
def test_native_diagnostic_one_attempt_preserves_original_and_never_continues(
    db, tmp_path, mode, code
):
    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        view = request_view(db, pid, rid, worker.profile, target)
        assert (
            view["diagnostic_available"]
            and view["resume_block_reason"] == "analysis_resume_request_fix_required"
        )
        assert (
            prepare_resume(worker, pid, cid, rid)["error_code"]
            == "analysis_resume_request_fix_required"
        )
        prep = prepare_diagnostic(worker, pid, cid, rid)
        assert prep["state"] == "ready", prep
        document = worker.admission.transfer(pid, str(prep["id"]), download=True)
        diag = document["diagnostic_request"]
        assert (
            diag["wire_digest"] == calls[-1]["payload_digest"]
            and diag["wire_bytes"] == target["wire_bytes"]
        )
        assert (
            diag["max_new_calls"] == 1
            and diag["automatic_retries"] == 0
            and not diag["continue_analysis"]
        )
        assert prep["input_digest"] == before["input_digest"]
        assert prep["consent_digest"] != before["consent"]["consent_digest"]
        qpath = directory / "diagnostic-quota.json"
        count = json.loads(qpath.read_text())["count"]
        control.write_text(mode)
        body = submission(prep)
        submitted = worker.admission.submit(pid, body)
        worker.start()
        done = terminal(worker, pid, str(submitted["id"]))
        assert done["state"] == "failed" and done["error_code"] == code and done["call_seq"] == 1
        ledger = db.all("SELECT * FROM analysis_calls WHERE request_id=%s", (done["id"],))
        assert len(ledger) == 1 and ledger[0]["phase"] == "reconcile"
        assert ledger[0]["payload_digest"] == target["wire_digest"]
        assert ledger[0]["outcome"] == ("responded" if mode == "200" else "failed")
        assert ledger[0]["error_code"] == (None if mode == "200" else code)
        assert json.loads(qpath.read_text())["count"] == count + 1
        entries = [
            json.loads(line) for line in (directory / "entries.jsonl").read_text().splitlines()
        ]
        assert (
            len(entries) == 4
            and entries[-1]["task"] == "reconcile"
            and entries[-1]["digest"] == target["wire_digest"]
        )
        assert (
            entries[-1]["endpoint"]
            == f"https://generativelanguage.googleapis.com/v1beta/models/{target['model']}:generateContent"
        )
        assert original(db, rid) == before and read_rows(db, pid, rid) == rows
        assert (
            db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (rid,)) == calls
        )
        copied = read_rows(db, pid, str(done["id"]))
        assert len(copied) == 2 and all(
            c["origin_request_id"] == r["origin_request_id"]
            for c, r in zip(copied, rows, strict=True)
        )
        assert not Experiences(db).listing(pid)
        assert (
            worker.admission.submit(pid, body)["id"] == done["id"]
        )  # Same-ID retry is observation only.
        assert prepare_diagnostic(worker, pid, cid, rid)["error_code"] == "analysis_resume_consumed"
        assert (
            prepare_resume(worker, pid, cid, str(done["id"]))["error_code"]
            == "analysis_diagnostic_consumed"
        )
        assert not request_view(db, pid, rid, worker.profile, target)["diagnostic_available"]
        assert json.loads(qpath.read_text())["count"] == count + 1
    finally:
        stop(worker, owner)


def test_wire_mismatch_at_final_native_gate_sends_nothing_and_consumes_no_quota(db, tmp_path):
    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        prep = prepare_diagnostic(worker, pid, cid, rid)
        assert prep["state"] == "ready"
        quota = (directory / "diagnostic-quota.json").read_bytes()
        control.write_text("wire")
        submitted = worker.admission.submit(pid, submission(prep))
        worker.start()
        done = terminal(worker, pid, str(submitted["id"]))
        assert done["error_code"] == "analysis_diagnostic_wire_mismatch" and done["call_seq"] == 0
        assert not db.all("SELECT * FROM analysis_calls WHERE request_id=%s", (done["id"],))
        assert len((directory / "entries.jsonl").read_text().splitlines()) == 3
        assert (directory / "diagnostic-quota.json").read_bytes() == quota
        assert original(db, rid) == before and read_rows(db, pid, rid) == rows
        assert prepare_diagnostic(worker, pid, cid, rid)["error_code"] == "analysis_resume_consumed"
    finally:
        stop(worker, owner)


def test_parent_blocks_wrong_phase_digest_and_second_grant_and_checks_new_consent(db, tmp_path):
    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        prep = prepare_diagnostic(worker, pid, cid, rid)
        assert prep["state"] == "ready"
        wrong = submission(prep)
        wrong.confirmed = False
        assert worker.admission.submit(pid, wrong)["error_code"] == "analysis_consent_mismatch"
        submitted = worker.admission.submit(pid, submission(prep))
        request = str(submitted["id"])
        claimed = worker.state.claim()
        token = str(claimed["owner_token"])
        worker.state.child_ready(pid, request, token, [0, 0])
        for phase, digest in [("audit", target["wire_digest"]), ("reconcile", "a" * 64)]:
            with pytest.raises(CollectionError) as caught:
                worker.state.enter_call(pid, request, token, 1, phase, digest)
            assert caught.value.code == "analysis_diagnostic_wire_mismatch"
        worker.state.enter_call(pid, request, token, 1, "reconcile", target["wire_digest"])
        with pytest.raises(CollectionError) as caught:
            worker.state.enter_call(pid, request, token, 2, "reconcile", target["wire_digest"])
        assert caught.value.code == "analysis_diagnostic_wire_mismatch"
        worker.state.finish(pid, request, token, code="analysis_stopped")
        assert (
            db.one("SELECT * FROM analysis_calls WHERE request_id=%s", (request,))["error_code"]
            == "analysis_call_outcome_unknown"
        )
        assert original(db, rid) == before and read_rows(db, pid, rid) == rows
    finally:
        stop(worker, owner)


def test_queued_cancel_reserves_opportunity_without_provider_call_even_after_restart(db, tmp_path):
    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        prep = prepare_diagnostic(worker, pid, cid, rid)
        submitted = worker.admission.submit(pid, submission(prep))
        request = str(submitted["id"])
        quota = (directory / "diagnostic-quota.json").read_bytes()
        worker.state.cancel(pid, request)
        worker.start()
        assert terminal(worker, pid, request)["state"] == "cancelled"
        assert (directory / "diagnostic-quota.json").read_bytes() == quota
        assert prepare_diagnostic(worker, pid, cid, rid)["error_code"] == "analysis_resume_consumed"
    finally:
        stop(worker, owner)


def test_prepare_rejects_other_target_and_tampered_preview_without_dispatch(db, tmp_path):
    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        worker.admission.diagnostic_target = {**target, "project_id": str(uuid4())}
        assert (
            prepare_diagnostic(worker, pid, cid, rid)["error_code"]
            == "analysis_diagnostic_unavailable"
        )
        worker.admission.diagnostic_target = target
        prep = prepare_diagnostic(worker, pid, cid, rid)
        assert prep["state"] == "ready"
        candidate = worker.admission._cached(str(prep["id"]))
        worker.admission.cache[str(prep["id"])] = replace(
            candidate, diagnostic_wire=candidate.diagnostic_wire + b" "
        )
        with pytest.raises(CollectionError) as caught:
            worker.admission.transfer(pid, str(prep["id"]))
        assert caught.value.code == "analysis_diagnostic_wire_mismatch"
        assert original(db, rid) == before and read_rows(db, pid, rid) == rows
        assert len((directory / "entries.jsonl").read_text().splitlines()) == 3
    finally:
        stop(worker, owner)


def test_private_target_configuration_is_opt_in_and_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_LOG_GEMINI_DIAGNOSTIC_TARGET_FILE", raising=False)
    assert configured_target() is None
    path = tmp_path / "target.json"
    path.write_text("{}")
    path.chmod(0o600)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_DIAGNOSTIC_TARGET_FILE", str(path))
    assert configured_target() is None
    link = tmp_path / "link"
    link.symlink_to(path)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_DIAGNOSTIC_TARGET_FILE", str(link))
    assert configured_target() is None


@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
def test_inflight_cancel_or_shutdown_preserves_unknown_call_and_no_second_opportunity(
    db, tmp_path, ending
):
    from test_analysis_worker import wait_for

    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    closed = False
    try:
        prep = prepare_diagnostic(worker, pid, cid, rid)
        control.write_text("blocked")
        submitted = worker.admission.submit(pid, submission(prep))
        request = str(submitted["id"])
        worker.start()
        wait_for(lambda: len((directory / "entries.jsonl").read_text().splitlines()) == 4)
        handle = worker.child
        assert handle and handle.diagnostic and handle.process.is_alive()
        if ending == "cancel":
            worker.state.cancel(pid, request)
            assert terminal(worker, pid, request)["state"] == "cancelled"
        else:
            stop(worker, owner)
            closed = True
            assert original(db, request)["error_code"] == "failed_server_stopped"
        assert not handle.process.is_alive()
        ledger = db.one("SELECT * FROM analysis_calls WHERE request_id=%s", (request,))
        assert ledger["error_code"] == "analysis_call_outcome_unknown"
        assert ledger["outcome"] == "failed"
        assert json.loads((directory / "diagnostic-quota.json").read_text())["count"] == 4
        assert original(db, rid) == before and read_rows(db, pid, rid) == rows
        assert len(read_rows(db, pid, request)) == 2 and not Experiences(db).listing(pid)
        if not closed:
            assert (
                prepare_diagnostic(worker, pid, cid, rid)["error_code"]
                == "analysis_resume_consumed"
            )
    finally:
        if not closed:
            stop(worker, owner)


def test_diagnostic_history_eligibility_never_decodes_fixed_input_snapshot(
    db, tmp_path, monkeypatch
):
    from psycopg.types.json import set_json_loads

    pid, cid, store, owner, worker, directory, rid, target, before, calls, rows, control = (
        diagnostic_setup(db, tmp_path)
    )
    try:
        connect = db.connect

        def metadata_connection():
            conn = connect()

            def metadata_json(raw):
                value = json.loads(raw)
                if isinstance(value, dict) and "entries" in value:
                    raise AssertionError(
                        "History projection must not decode the fixed input snapshot"
                    )
                return value

            set_json_loads(metadata_json, conn)
            return conn

        with monkeypatch.context() as patch:
            patch.setattr(db, "connect", metadata_connection)
            view = request_view(db, pid, rid, worker.profile, target)
        assert view["diagnostic_available"] is True
        assert view["resume_block_reason"] == "analysis_resume_request_fix_required"
        assert "input_payload" not in view and "_input_digest" not in view
    finally:
        stop(worker, owner)
