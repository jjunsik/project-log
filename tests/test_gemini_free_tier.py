"""Free quota ownership: synthetic transport, fake clock, temporary state/DB only."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from test_experience_gemini import ModelConnection, model
from test_experiences import evidence, inputs
from test_gemini import FakeConnection

from project_log.experience_gemini import GeminiExperienceModel
from project_log.experiences import Discovery, ExperienceGraph
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import PACIFIC, RPD, RPM, TPM, FreeTierQuota, from_environment
from project_log.git import CollectionError

RAW = b'{"contents":[{"parts":[{"text":"synthetic input"}]}]}'


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now, self.waits = now, []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        assert seconds > 0
        self.waits.append(seconds)
        self.now += seconds
        return False


def quota(tmp_path, clock=None, **kwargs):
    clock = clock or Clock()
    return FreeTierQuota(tmp_path / "quota.json", clock=clock, wait=clock.wait, **kwargs)


def state(q):
    return json.loads(q.path.read_text())


def test_rpm_burst_waits_only_until_oldest_reservation_expires_and_survives_restart(tmp_path):
    clock, calls = Clock(), []
    q = quota(tmp_path, clock)
    for _ in range(RPM):
        q.reserve(RAW, lambda: calls.append(clock()))
    clock.now += 17
    restarted = quota(tmp_path, clock)
    restarted.reserve(RAW, lambda: calls.append(clock()))
    assert clock.waits == [43]
    assert calls[-1] - calls[0] == 60 and len(calls) == 16
    assert state(q)["count"] == 16
    assert "synthetic input" not in q.path.read_text()
    assert q.path.stat().st_mode & 0o777 == 0o600


def test_tpm_covers_whole_wire_and_reconciles_validated_usage_without_fixed_delay(tmp_path):
    clock = Clock()
    q = quota(tmp_path, clock)
    large = json.dumps(
        {"contents": [{"parts": [{"text": "한글😀" * 20_000}]}]}, ensure_ascii=False
    ).encode()
    assert 200_000 < q.estimate(large) < TPM
    reservation = q.reserve(large, lambda: None)
    q.usage(reservation, 10)
    q.reserve(large, lambda: None)
    assert not clock.waits
    q.reserve(RAW, lambda: None)
    q.reserve(large, lambda: None)
    assert clock.waits == [60]


def test_oversized_or_unestimated_input_stops_without_gate_or_reservation(tmp_path):
    q = quota(tmp_path)
    for raw, code in [
        (
            json.dumps({"contents": [{"parts": [{"text": "x" * TPM}]}]}).encode(),
            "gemini_free_request_limit",
        ),
        (
            b'{"contents":[{"parts":[{"inlineData":{"data":"AA=="}}]}]}',
            "gemini_free_tokens_unknown",
        ),
    ]:
        with pytest.raises(CollectionError) as exc:
            q.reserve(raw, lambda: pytest.fail("No worker call entry allowed"))
        assert exc.value.code == code
    assert not q.path.exists()


def test_daily_limit_no_next_day_wait_and_pacific_reset_keeps_minute_window(tmp_path):
    clock = Clock()
    q = quota(tmp_path, clock)
    for _ in range(RPD):
        q.reserve(RAW, lambda: None)
    waits = clock.waits.copy()
    with pytest.raises(CollectionError) as exc:
        quota(tmp_path, clock).reserve(RAW, lambda: pytest.fail("Daily limit must block entry"))
    assert exc.value.code == "gemini_free_daily_limit"
    assert clock.waits == waits and state(q)["count"] == RPD
    clock.now = datetime(2027, 1, 16, 23, 59, 50, tzinfo=PACIFIC).timestamp()
    for _ in range(RPM):
        q.reserve(RAW, lambda: None)
    clock.now += 10
    q.reserve(RAW, lambda: None)
    assert clock.waits[-1] == 50 and state(q)["count"] == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt", "clock"])
def test_restart_damaged_or_regressed_state_fails_closed(tmp_path, damage):
    clock = Clock()
    q = quota(tmp_path, clock)
    q.reserve(RAW, lambda: None)
    if damage == "missing":
        q.path.unlink()
    elif damage == "corrupt":
        q.path.write_text("{partial crash write")
    else:
        clock.now -= 1
    with pytest.raises(CollectionError) as exc:
        quota(tmp_path, clock).reserve(RAW, lambda: pytest.fail("Invalid state must block entry"))
    assert exc.value.code == "gemini_free_state_unavailable"


def test_cancelled_wait_and_denied_worker_gate_do_not_consume_new_attempt(tmp_path):
    clock, stopped = Clock(), threading.Event()
    q = quota(tmp_path, clock, stopped=stopped)
    for _ in range(RPM):
        q.reserve(RAW, lambda: None)

    def cancel(seconds):
        stopped.set()
        return True

    q.wait = cancel
    with pytest.raises(CollectionError) as exc:
        q.reserve(RAW, lambda: pytest.fail("Cancelled wait must not enter call"))
    assert exc.value.code == "analysis_stopped" and state(q)["count"] == RPM
    clock.now += 60

    def deny():
        raise CollectionError("analysis_stopped", "Synthetic worker denial")

    with pytest.raises(CollectionError):
        quota(tmp_path, clock).reserve(RAW, deny)
    assert state(q)["count"] == RPM


def test_shared_file_serializes_reservations(tmp_path):
    clock = Clock()
    q = quota(tmp_path, clock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: quota(tmp_path, clock).reserve(RAW, lambda: None), range(12)))
    assert len(set(ids)) == state(q)["count"] == 12


def test_opt_in_disabled_is_inert_and_live_factory_connects_only_explicit_configuration(
    tmp_path, monkeypatch
):
    from project_log.analysis_child import gemini_model

    monkeypatch.setattr(
        GeminiConfig,
        "from_environment",
        lambda **_: GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
    )
    profile = {"mode": "live", "free_tier_confirmed": True, "model": "gemini-3.5-flash-lite"}
    monkeypatch.delenv("PROJECT_LOG_GEMINI_FREE_LIMITS", raising=False)
    assert from_environment(stopped=threading.Event()) is None
    assert gemini_model(profile, lambda _: None, stopped=threading.Event()).quota is None
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_LIMITS", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_LIMITS_STATE", str(tmp_path / "quota.json"))
    stopped = threading.Event()
    adapter = gemini_model(
        profile,
        lambda _: None,
        stopped=stopped,
    )
    assert isinstance(adapter, GeminiExperienceModel) and adapter.quota.stopped is stopped
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_LIMITS_STATE", "relative.json")
    with pytest.raises(CollectionError):
        from_environment(stopped=stopped)


def test_failed_reservation_write_prevents_http_and_preserves_prior_count(tmp_path, monkeypatch):
    q = quota(tmp_path)
    q.reserve(RAW, lambda: None)
    connection = ModelConnection(retries=0, verify=True, trust_env=False)

    def fail_replace(*args):
        raise OSError("Synthetic atomic write failure")

    monkeypatch.setattr("project_log.gemini_free_tier.os.replace", fail_replace)
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        test_transport_factory=lambda: connection,
        quota=q,
    )
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "Synthetic instruction", (), "{}", Discovery)
    assert exc.value.code == "gemini_free_state_unavailable"
    assert not connection.requests and state(q)["count"] == 1
    assert not list(tmp_path.glob(".gemini-quota-*"))


def test_real_sdk_graph_payload_and_results_unchanged_when_quota_enabled(tmp_path):
    snapshot = inputs(evidence("one", "Synthetic counter returns zero"))
    baseline = model("gemini-3.5-flash-lite")
    result = ExperienceGraph(baseline).run(snapshot)
    bodies = [c.body for c in ModelConnection.calls]
    adapter = model("gemini-3.5-flash-lite")
    adapter.quota = quota(tmp_path)
    actual = ExperienceGraph(adapter).run(snapshot)
    assert actual == result and adapter.usage == baseline.usage
    assert [c.body for c in ModelConnection.calls] == bodies
    assert state(adapter.quota)["count"] == actual.model_calls


@pytest.mark.parametrize(
    "kind", ["minute", "daily", "unknown", "network", "503", "invalid", "429_read"]
)
def test_sdk_failure_no_429_or_ambiguous_resend_and_persistent_attempts(tmp_path, kind):
    q = quota(tmp_path)
    connection = FakeConnection(retries=0, verify=True, trust_env=False)
    connection.status = 429
    connection.payload = b'{"error":{"details":[]}}'
    expected = "provider_rate_limited"
    if kind in {"minute", "daily"}:
        period = "Day" if kind == "daily" else "Minute"
        connection.payload = json.dumps(
            {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": "GenerateRequestsPer" + period}],
                        }
                    ]
                }
            }
        ).encode()
        expected = "gemini_free_daily_limit" if kind == "daily" else "provider_minute_limit"
    elif kind == "network":
        connection.error = httpx.ReadTimeout("Synthetic uncertain transmission")
        expected = "provider_read_timeout_transport_hunknown"
    elif kind == "503":
        connection.status = 503
        expected = "provider_temporarily_unavailable"
    elif kind == "invalid":
        connection.status, connection.payload = 200, b"invalid response"
        expected = "provider_invalid_response_envelope_h200"
    elif kind == "429_read":
        connection.chunks = [b'{"error":']
        connection.stream_error = httpx.ReadTimeout("Synthetic private 429 body")
        expected = "provider_rate_limited"  # Diagnostic failure preserves observed 429.
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        test_transport_factory=lambda: connection,
        quota=q,
    )
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "Synthetic instruction", (), "{}", Discovery)
    assert exc.value.code == expected
    assert len(connection.requests) == state(q)["count"] == 1
    assert connection.closed
    if kind == "daily":
        with pytest.raises(CollectionError) as exc:
            quota(tmp_path).reserve(RAW, lambda: pytest.fail("Known daily exhaustion persists"))
        assert exc.value.code == "gemini_free_daily_limit"


def free_worker_factory(profile, gate):
    """Spawn fixture, with exactly the normal SDK transport and existing call gate."""
    directory = Path(profile["test_directory"])
    mode = profile["test_mode"]
    stopped = gate.__self__.stopped
    clock = Clock()
    q = quota(directory, clock, stopped=stopped)
    if mode in {"free_cancel", "free_restart"}:
        for _ in range(RPM):
            q.reserve(RAW, lambda: None)

        def waiting(seconds):
            (directory / "quota-wait").touch()
            return stopped.wait(seconds)

        q.wait = waiting
    if mode == "free_daily":
        q.reserve(RAW, lambda: None)
        saved = state(q)
        saved["count"] = RPD
        q.save(saved)

    calls = []

    def transport():
        connection = ModelConnection(retries=0, verify=True, trust_env=False)
        if mode == "free_429":
            connection.status = 429
        if mode == "free_503" or (mode == "free_503once" and not calls):
            connection.status = 503
        calls.append(connection)
        return connection

    return GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        before_send=gate,
        test_transport_factory=transport,
        quota=q,
    )


@pytest.mark.parametrize(
    "mode,expected,calls",
    [
        ("free_success", "completed", None),
        ("free_daily", "failed", 0),
        ("free_429", "failed", 1),
        ("free_503", "failed", 2),
        ("free_503once", "completed", None),
    ],
)
def test_worker_quota_success_or_failure_keeps_existing_result_and_attempt_policy(
    db, tmp_path, mode, expected, calls
):
    from test_analysis_worker import accepted, setup_worker, stop, terminal

    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, mode)
    worker.factory = free_worker_factory
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        row = terminal(worker, pid, request)
        assert row["state"] == expected
        actual = len(db.all("SELECT * FROM analysis_calls WHERE request_id=%s", (request,)))
        assert calls is None or actual == calls
        recorded = json.loads((directory / "quota.json").read_text())["count"]
        assert recorded == (RPD if mode == "free_daily" else actual)
        assert bool(row["result_run_id"]) == (expected == "completed")
        if mode == "free_daily":
            assert row["error_code"] == "gemini_free_daily_limit"
        if mode in {"free_503", "free_503once"}:
            ledger = db.all("SELECT * FROM analysis_calls ORDER BY seq")
            assert ledger[0]["error_code"] == "provider_temporarily_unavailable"
            assert ledger[0]["payload_digest"] == ledger[1]["payload_digest"]
            assert (ledger[1]["entered_at"] - ledger[0]["finished_at"]).total_seconds() >= 30
            assert ledger[1]["outcome"] == ("failed" if mode == "free_503" else "responded")
            if mode == "free_503once":
                run = db.one(
                    "SELECT result FROM experience_runs WHERE id=%s", (row["result_run_id"],)
                )
                assert actual == run["result"]["model_calls"] + 1
        store.add(pid, "released.md", b"Synthetic unlocked after terminal result")
    finally:
        stop(worker, owner)


@pytest.mark.parametrize("mode", ["free_cancel", "free_restart"])
def test_worker_cancel_or_shutdown_while_quota_waits_sends_no_call(db, tmp_path, mode):
    from test_analysis_worker import accepted, setup_worker, stop, terminal, wait_for

    from project_log.analysis_worker import AnalysisWorker

    pid, cid, store, owner, worker, directory = setup_worker(db, tmp_path, mode)
    worker.factory = free_worker_factory
    try:
        request, _ = accepted(worker, pid, cid)
        worker.start()
        wait_for(lambda: (directory / "quota-wait").exists())
        if mode == "free_cancel":
            worker.state.cancel(pid, request)
            assert terminal(worker, pid, request)["state"] == "cancelled"
        else:
            worker.close()
            row = worker.admission.request(pid, request)
            assert row["state"] == "failed" and row["error_code"] == "failed_server_stopped"
        assert db.all("SELECT * FROM analysis_calls") == []
        assert json.loads((directory / "quota.json").read_text())["count"] == RPM
        if mode == "free_restart":
            restarted = AnalysisWorker(
                db,
                store,
                owner,
                profile={**worker.profile, "test_mode": "free_success"},
                model_factory=free_worker_factory,
            )
            restarted.recover()
            restarted.state.open()
            try:
                assert restarted.once() is False  # No automatic request/analysis restart.
                next_request, _ = accepted(restarted, pid, cid)
                restarted.start()
                assert terminal(restarted, pid, next_request)["state"] == "completed"
                actual = len(db.all("SELECT * FROM analysis_calls"))
                assert json.loads((directory / "quota.json").read_text())["count"] == RPM + actual
                assert restarted.admission.request(pid, request)["state"] == "failed"
            finally:
                restarted.close()
    finally:
        stop(worker, owner)
