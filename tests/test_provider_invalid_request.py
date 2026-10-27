"""Observed HTTP 400 boundaries; no emulation of Google's schema compiler."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_analysis_admission import submission
from test_analysis_resume import original, prepare_resume
from test_analysis_worker import accepted, setup_worker, stop, terminal
from test_experience_gemini import ModelConnection
from test_gemini import FakeConnection
from test_multiday_analysis import FakeControl

from project_log.analysis_api import request_view
from project_log.analysis_checkpoints import previous_budget_profile, read_rows
from project_log.analysis_child import ControlledModel
from project_log.analysis_execution import execution_profile
from project_log.analysis_recovery import resume_block
from project_log.experience_gemini import INVALID_REQUEST_DETAILS_BYTES, GeminiExperienceModel
from project_log.experience_store import Experiences
from project_log.experiences import Discovery
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import FreeTierQuota
from project_log.git import CollectionError


def error_body(message, **fields):
    return json.dumps(
        {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message, **fields}}
    ).encode()


@pytest.mark.parametrize(
    "payload,expected",
    [
        (error_body("A schema has too many states; PRIVATE"), "_schema_complexity"),
        (error_body("A schema has too much branching; PRIVATE"), "_schema_complexity"),
        (
            error_body("A schema exceeds the maximum allowed nesting depth. PRIVATE"),
            "_schema_depth",
        ),
        (error_body("Request payload size exceeds the limit: PRIVATE"), "_request_size"),
        (error_body("Unrecognized reason PRIVATE"), ""),
        (error_body("A schema has too many states", status="UNRECOGNIZED_PRIVATE"), ""),
        (b"PRIVATE invalid JSON", ""),
        (error_body("A schema has too many states " + "PRIVATE" * 2000), ""),
    ],
)
def test_native_400_is_one_actual_attempt_no_retry_or_checkpoint(tmp_path, payload, expected):
    class BadRequest(FakeConnection):
        status = 400

    BadRequest.payload = payload
    BadRequest.calls = []
    quota = FreeTierQuota(tmp_path / "quota.json")
    control = FakeControl()
    model = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-test-key", True),
        test_transport_factory=lambda: BadRequest(retries=0, verify=True, trust_env=False),
        before_send=control.enter,
        quota=quota,
    )
    wrapped = ControlledModel(model, control, execution_profile("gemini-3.5-flash-lite"))
    with pytest.raises(CollectionError) as caught:
        wrapped.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_invalid_request" + expected
    assert "PRIVATE" not in str(caught.value) + caught.value.code
    assert control.seq == 1 and control.waits == []
    assert len(BadRequest.calls) == 1 and len(BadRequest.calls[0].requests) == 1
    assert BadRequest.calls[0].stream.closed and BadRequest.calls[0].closed
    assert json.loads(quota.path.read_text())["count"] == 1
    assert "PRIVATE" not in quota.path.read_text()
    assert model.usage == []


@pytest.mark.parametrize("failure", ["stream", "close", "encoding", "bounded"])
def test_diagnostic_failure_preserves_http400_without_retry(tmp_path, failure):
    class Broken(FakeConnection):
        status = 400
        payload = b"PRIVATE"

    if failure == "stream":
        Broken.stream_error = httpx.ReadTimeout("PRIVATE")
    elif failure == "close":
        Broken.stream_close_error = RuntimeError("PRIVATE")
        Broken.close_error = RuntimeError("PRIVATE")
    elif failure == "encoding":
        Broken.response_headers = {"content-encoding": "gzip"}
    else:
        Broken.chunks = [b"x" * (INVALID_REQUEST_DETAILS_BYTES + 1), b"PRIVATE unread"]
    Broken.calls = []
    model = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-test-key", True),
        test_transport_factory=lambda: Broken(retries=0, verify=True, trust_env=False),
    )
    with pytest.raises(CollectionError) as caught:
        model.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_invalid_request"
    assert "PRIVATE" not in str(caught.value)
    assert len(Broken.calls) == 1 and Broken.calls[0].stream.closed
    if failure == "bounded":
        assert Broken.calls[0].stream.yielded == 1


@pytest.mark.parametrize(
    "code", ["provider_invalid_request", "provider_invalid_request_schema_depth"]
)
@pytest.mark.parametrize("budget_transition", [False, True])
def test_http400_does_not_unlock_tomorrow_or_after_budget_only_transition(code, budget_transition):
    profile = execution_profile("synthetic")
    failed = {
        "error_code": code,
        "consent": {
            "descriptor": {
                "profile": previous_budget_profile(profile) if budget_transition else profile
            }
        },
        "finished_at": datetime.now(UTC),
    }
    assert (
        resume_block(None, failed, profile, now=failed["finished_at"] + timedelta(days=2))
        == "analysis_resume_request_fix_required"
    )


def invalid_request_factory(profile, gate):
    """Spawn-importable synthetic native SDK connection, never a real provider."""

    class RejectReview(ModelConnection):
        def handle_request(self, request):
            prompt = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
            if prompt["task"] == "reconcile":
                self.status = 400
                self.payload = error_body("A schema has too many states PRIVATE")
                return FakeConnection.handle_request(self, request)
            return super().handle_request(request)

    return GeminiExperienceModel(
        GeminiConfig(profile["model"], "synthetic-test-key", True),
        test_transport_factory=lambda: RejectReview(retries=0, verify=True, trust_env=False),
        before_send=gate,
        quota=FreeTierQuota(Path(profile["test_directory"]) / "quota400.json"),
    )


def test_isolated_native400_preserves_prefix_calls_and_blocks_prepare_and_cached_consent(
    db, tmp_path, monkeypatch
):
    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, mode="sdk_badrequest")
    worker.factory = invalid_request_factory
    try:
        store.add(pid, "second.md", b"Another synthetic counter correction")
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        assert failed["error_code"] == "provider_invalid_request_schema_complexity"
        saved, before = read_rows(db, pid, rid), original(db, rid)
        calls = db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (rid,))
        assert [c["phase"] for c in calls] == ["discover", "consolidate", "reconcile"]
        assert [c["outcome"] for c in calls] == ["responded", "responded", "failed"]
        assert [r["task"] for r in saved] == ["discover", "consolidate"]
        quota = (directory / "quota400.json").read_bytes()
        assert json.loads(quota)["count"] == failed["call_seq"] == 3
        assert not Experiences(db).listing(pid)
        assert (
            request_view(db, pid, rid, worker.profile)["resume_block_reason"]
            == "analysis_resume_request_fix_required"
        )
        assert (
            prepare_resume(worker, pid, cid, rid)["error_code"]
            == "analysis_resume_request_fix_required"
        )
        # A ready preparation issued under the old guard cannot bypass submit's
        # fresh source/recovery checks. Original input and new consent still bind it.
        with monkeypatch.context() as prior:
            prior.setattr("project_log.analysis_admission.resume_block", lambda *args: None)
            prep = prepare_resume(worker, pid, cid, rid)
        assert prep["state"] == "ready" and prep["input_digest"] == before["input_digest"]
        rejected = worker.admission.submit(pid, submission(prep))
        assert rejected["state"] == "rejected"
        assert rejected["error_code"] == "analysis_resume_request_fix_required"
        assert original(db, rid) == before and read_rows(db, pid, rid) == saved
        assert (
            db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (rid,)) == calls
        )
        assert (directory / "quota400.json").read_bytes() == quota
        assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == 3
        store.add(pid, "released.md", b"Project unlocked after failure")
    finally:
        stop(worker, owner)
