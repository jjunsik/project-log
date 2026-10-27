"""Finite Free Tier executions, manual day changes and immutable prefix replay."""

import json
from datetime import datetime, timedelta

import httpx
import pytest
from test_analysis_admission import submission
from test_analysis_checkpoints import MemoryCheckpoints
from test_analysis_resume import original, prepare_resume
from test_analysis_worker import accepted, setup_worker, stop, terminal
from test_experience_gemini import ModelConnection
from test_experiences import OfflineModel, evidence, inputs
from test_gemini import FakeConnection
from test_gemini_free_tier import RAW, Clock

from project_log.analysis_api import request_view
from project_log.analysis_checkpoints import (
    BUDGET_COMPATIBILITY,
    compatibility_policy,
    contract_digest,
    previous_budget_profile,
    read_rows,
    resume_rows,
    validate_rows,
)
from project_log.analysis_child import ControlledModel
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_recovery import daily_reset, resume_block
from project_log.experience_gemini import GeminiExperienceModel, TransientProviderError
from project_log.experience_input import Evidence, Media
from project_log.experience_store import Experiences
from project_log.experiences import Discovery, ExperienceGraph
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import PACIFIC, FreeTierQuota, MinuteProviderError
from project_log.git import CollectionError
from project_log.policy import sha256


def test_603_calls_in_three_provider_days_replay_more_than_512_successes_free(tmp_path):
    # Distinct media enforce separate discovery batches. Only the last text entry
    # yields a seed; empty discoveries remain valid, checkpointed model decisions.
    snapshot = inputs(
        *[
            Evidence(str(i), "{}", media=(Media(str(i) + ".png", "image/png", str(i).encode()),))
            for i in range(599)
        ],
        evidence("last", "counter correction"),
    )
    journal, attempts = MemoryCheckpoints(), []
    clock = Clock(datetime(2027, 1, 15, 12, tzinfo=PACIFIC).timestamp())
    q = FreeTierQuota(tmp_path / "quota.json", clock=clock, wait=clock.wait)

    class Model(OfflineModel):
        def complete(self, task, *args):
            q.reserve(RAW, lambda: attempts.append(task))
            if task == "discover" and args[1][0].media:
                return Discovery(seeds=[])
            return super().complete(task, *args)

    counts, prefixes = [], []
    for day in range(3):
        if day:
            clock.now = datetime(2027, 1, 15 + day, 12, tzinfo=PACIFIC).timestamp()
        # Synthetic pre-existing usage by another client of the same project.
        for _ in range(290):
            q.reserve(RAW, lambda: None)
        before = len(attempts)
        if day < 2:
            with pytest.raises(CollectionError, match="일일") as caught:
                ExperienceGraph(Model(), checkpoints=journal).run(snapshot)
            assert caught.value.code == "gemini_free_daily_limit"
        else:
            result = ExperienceGraph(Model(), checkpoints=journal).run(snapshot)
            assert result.model_calls == 603 and len(result.candidates) == 1
        assert len(attempts) - before <= 512
        counts.append(json.loads(q.path.read_text())["count"])
        prefixes.append(len(journal.rows))
    assert prefixes == [210, 420, 603] and counts == [500, 500, 473]
    assert len(attempts) == len(journal.rows) == 603
    assert attempts.count("discover") == 600
    # Journal bounds are response bytes, not a cumulative 512-entry ceiling.
    profile = execution_profile("synthetic")
    rows = [
        dict(
            ordinal=i,
            task=t,
            request_digest=d,
            response=json.loads(raw),
            response_digest=sha256(raw.encode()),
            contract_digest=contract_digest(snapshot.digest, profile),
        )
        for i, (t, d, raw) in enumerate(journal.rows, 1)
    ]
    validate_rows(rows, snapshot.digest, profile)
    again = ExperienceGraph(Model(), checkpoints=journal, remaining_attempts=lambda: 0).run(
        snapshot
    )
    assert again == result and len(attempts) == 603


def test_budget_only_transition_preserves_all_completed_stages_and_rejects_other_changes():
    snapshot = inputs(evidence("one", "counter correction"))
    new = execution_profile("synthetic")
    old = previous_budget_profile(new)
    journal = MemoryCheckpoints()
    ExperienceGraph(OfflineModel(), checkpoints=journal).run(snapshot)
    rows = [
        dict(
            ordinal=i,
            task=t,
            request_digest=d,
            response=json.loads(raw),
            response_digest=sha256(raw.encode()),
            contract_digest=contract_digest(snapshot.digest, old),
        )
        for i, (t, d, raw) in enumerate(journal.rows, 1)
    ]
    copied = resume_rows(rows, snapshot, old, new)
    assert compatibility_policy(old, new) == BUDGET_COMPATIBILITY
    assert [r["response_digest"] for r in copied] == [r["response_digest"] for r in rows]
    assert copied[-1]["task"] == "assess"
    assert rows[-1]["contract_digest"] == contract_digest(snapshot.digest, old)
    for change in ({"model": "other"}, {"instruction_digest": "a" * 64}, {"retry_delay": 1}):
        with pytest.raises(CollectionError):
            resume_rows(rows, snapshot, {**old, **change}, new)


@pytest.mark.parametrize("month,hour", [(1, 17), (7, 16)])
def test_provider_midnight_reset_uses_dst_not_fixed_korea_hour(month, hour):
    from zoneinfo import ZoneInfo

    reset = daily_reset(datetime(2027, month, 15, 12, tzinfo=PACIFIC))
    assert reset.hour == 0 and reset.astimezone(ZoneInfo("Asia/Seoul")).hour == hour


class FakeControl:
    def __init__(self):
        self.seq = self.entered_seq = 0
        self.waits = []
        self.stopped = type(
            "Stop", (), {"wait": lambda _, delay: self.waits.append(delay) or False}
        )()

    def check(self):
        pass

    def enter(self, digest):
        self.seq += 1
        self.entered_seq = self.seq

    def send(self, **event):
        pass

    def retry_wait(self, delay):
        self.waits.append(delay)


@pytest.mark.parametrize(
    "errors,expected,attempts,waits",
    [
        ([MinuteProviderError(2)], None, 2, [2]),
        ([MinuteProviderError(61)], "provider_minute_limit", 1, []),
        ([MinuteProviderError(2), MinuteProviderError(2)], "provider_minute_limit", 2, [2]),
        ([TransientProviderError(), MinuteProviderError(2)], "provider_minute_limit", 2, [30]),
        ([CollectionError("provider_network_error", "synthetic")], "provider_network_error", 1, []),
        (
            [CollectionError("gemini_free_daily_limit", "synthetic")],
            "gemini_free_daily_limit",
            1,
            [],
        ),
    ],
)
def test_only_explicit_minute_429_gets_one_bounded_retry(errors, expected, attempts, waits):
    class Model:
        def complete(self, *args):
            if errors:
                raise errors.pop(0)
            return Discovery(seeds=[])

    control = FakeControl()
    wrapped = ControlledModel(Model(), control, execution_profile("synthetic"))
    if expected:
        with pytest.raises(CollectionError) as caught:
            wrapped.complete("discover", "synthetic", (), "{}", Discovery)
        assert caught.value.code == expected
    else:
        assert wrapped.complete("discover", "synthetic", (), "{}", Discovery) == Discovery(seeds=[])
    assert control.seq == attempts and control.waits == waits


@pytest.mark.parametrize(
    "period,expected",
    [
        ("PerMinute", "provider_minute_limit"),
        ("PerDay", "gemini_free_daily_limit"),
        ("Unknown", "provider_rate_limited"),
        ("PerMinuteAndPerDay", "gemini_free_daily_limit"),
    ],
)
def test_native_429_metadata_classification_does_not_persist_raw_error(tmp_path, period, expected):
    q = FreeTierQuota(tmp_path / "quota.json")
    raw = json.dumps(
        {
            "error": {
                "message": "private-provider-message",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": period}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"},
                ],
            }
        }
    ).encode()
    response = httpx.Response(429, stream=httpx.ByteStream(raw))
    with pytest.raises(CollectionError) as caught:
        q.reject_429(response)
    assert caught.value.code == expected
    if q.path.exists():
        assert "private-provider-message" not in q.path.read_text()


def test_isolated_sdk_three_manual_days_preserve_results_and_exceed_512_lineage(
    db, tmp_path, monkeypatch
):
    import project_log.analysis_recovery as recovery

    clockfile = tmp_path / "clock"
    clockfile.write_text(str(datetime(2027, 1, 15, 12, tzinfo=PACIFIC).timestamp()))

    class Date(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(float(clockfile.read_text()), tz)

    monkeypatch.setattr(recovery, "datetime", Date)
    modefile = tmp_path / "mode"
    modefile.write_text("checkpoint_sdk_success")
    pid, cid, store, owner, worker, directory = setup_worker(
        db, tmp_path, mode="sdk_checkpoint", control_file=modefile
    )
    profile = {**worker.profile, "test_quota_clock": str(clockfile)}
    worker.profile = worker.admission.profile = worker.state.profile = profile

    def wait(seconds):
        clockfile.write_text(str(float(clockfile.read_text()) + seconds))
        return False

    q = FreeTierQuota(
        directory / "quota.json", clock=lambda: float(clockfile.read_text()), wait=wait
    )
    history = []
    try:
        rid, _ = accepted(worker, pid, cid)
        # Explicit synthetic boundary fixture: previously inherited511 attempts.
        db.execute("UPDATE analysis_requests SET inherited_attempts=511 WHERE id=%s", (rid,))
        for day, prior in enumerate([499, 498, 499]):
            if day:
                clockfile.write_text(
                    str(datetime(2027, 1, 15 + day, 12, tzinfo=PACIFIC).timestamp())
                )
            for _ in range(prior):
                q.reserve(RAW, lambda: None)
            if day:
                prep = prepare_resume(worker, pid, cid, rid)
                assert prep["state"] == "ready", prep
                new = worker.admission.submit(pid, submission(prep))
                assert new["state"] == "queued"
                rid = str(new["id"])
            if not day:
                worker.start()
            done = terminal(worker, pid, rid)
            rows = read_rows(db, pid, rid)
            if day < 2:
                assert (
                    done["state"] == "failed" and done["error_code"] == "gemini_free_daily_limit"
                ), done
                assert not Experiences(db).listing(pid)
                db.execute(
                    "UPDATE analysis_requests SET finished_at=%s WHERE id=%s",
                    (Date.now(PACIFIC), rid),
                )
                assert (
                    prepare_resume(worker, pid, cid, rid)["error_code"]
                    == "analysis_resume_daily_wait"
                )
            else:
                assert done["state"] == "completed" and len(Experiences(db).listing(pid)) == 1, done
            history.append(original(db, rid))
            assert len(rows) == [1, 3, 4][day]
            assert done["call_seq"] == [1, 2, 1][day]
            if day:
                assert rows[0]["origin_request_id"] == history[0]["id"]
        assert [r["inherited_attempts"] for r in history] == [511, 512, 514]
        assert history[-1]["inherited_attempts"] + history[-1]["call_seq"] == 515
        assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == 4
        assert json.loads(q.path.read_text())["count"] == 500
        assert original(db, str(history[0]["id"])) == history[0]
        assert original(db, str(history[1]["id"])) == history[1]
    finally:
        stop(worker, owner)


def test_no_progress_three_resumes_block_same_day_and_keep_all_originals(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("network_stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        root = terminal(worker, pid, rid)
        count = len(read_rows(db, pid, rid))
        for _ in range(3):
            prep = prepare_resume(worker, pid, cid, rid)
            assert prep["state"] == "ready", prep
            rid = str(worker.admission.submit(pid, submission(prep))["id"])
            assert terminal(worker, pid, rid)["error_code"] == "provider_network_error"
            assert len(read_rows(db, pid, rid)) == count
        before = original(db, rid)
        blocked = prepare_resume(worker, pid, cid, rid)
        assert blocked["error_code"] == "analysis_resume_no_progress"
        assert original(db, rid) == before
        assert (
            request_view(db, pid, rid, worker.profile)["resume_block_reason"]
            == "analysis_resume_no_progress"
        )
        with db.connect() as conn:
            assert (
                resume_block(
                    conn, before, worker.profile, now=before["finished_at"] + timedelta(days=1)
                )
                is None
            )
        assert original(db, str(root["id"]))["state"] == "failed"
    finally:
        stop(worker, owner)


def test_semantic_failure_is_not_treated_as_tomorrows_quota(db, tmp_path):
    control = tmp_path / "mode"
    control.write_text("stage_audit")
    pid, cid, store, owner, worker, _ = setup_worker(db, tmp_path, control_file=control)
    try:
        rid, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, rid)
        before = original(db, rid)
        prep = prepare_resume(worker, pid, cid, rid)
        assert prep["error_code"] == "analysis_resume_validation_required"
        assert original(db, rid) == before
        with db.connect() as conn:
            assert (
                resume_block(
                    conn, before, worker.profile, now=failed["finished_at"] + timedelta(days=1)
                )
                == "analysis_resume_validation_required"
            )
    finally:
        stop(worker, owner)


def test_sdk_explicit_minute_retry_counts_two_actual_calls_and_no_hidden_retries(tmp_path):
    class Connection(ModelConnection):
        calls_seen = 0

        def handle_request(self, request):
            Connection.calls_seen += 1
            if Connection.calls_seen == 1:
                self.status = 429
                self.payload = json.dumps(
                    {
                        "error": {
                            "details": [
                                {
                                    "@type": "google.rpc.QuotaFailure",
                                    "violations": [{"quotaId": "PerMinute"}],
                                },
                                {"@type": "google.rpc.RetryInfo", "retryDelay": "2s"},
                            ]
                        }
                    }
                ).encode()
                return FakeConnection.handle_request(self, request)
            return super().handle_request(request)

    control = FakeControl()
    q = FreeTierQuota(tmp_path / "quota.json")
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        before_send=control.enter,
        quota=q,
        test_transport_factory=lambda: Connection(retries=0, verify=True, trust_env=False),
    )
    model = ControlledModel(adapter, control, execution_profile("gemini-3.5-flash-lite"))
    assert model.complete("discover", "synthetic", (), "{}", Discovery) == Discovery(seeds=[])
    assert Connection.calls_seen == control.seq == json.loads(q.path.read_text())["count"] == 2
    assert control.waits == [2]


def test_only_new_policy_can_replay_over_512_and_response_byte_bound_stays():
    profile = execution_profile("synthetic")
    response = {"seeds": []}
    rows = [
        dict(
            ordinal=i,
            task="discover",
            request_digest="a" * 64,
            response=response,
            response_digest=sha256(canonical(response).encode()),
            contract_digest=contract_digest("fixed", profile),
        )
        for i in range(1, 514)
    ]
    validate_rows(rows, "fixed", profile)
    legacy = previous_budget_profile(profile)
    legacy_rows = [{**r, "contract_digest": contract_digest("fixed", legacy)} for r in rows]
    with pytest.raises(CollectionError):
        validate_rows(legacy_rows, "fixed", legacy)
    small = {**profile, "result_max_bytes": 1}
    with pytest.raises(CollectionError):
        validate_rows(
            [{**rows[0], "contract_digest": contract_digest("fixed", small)}], "fixed", small
        )
