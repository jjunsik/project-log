"""Provider recovery contracts through the installed SDK; no live Gemini requests."""

import json
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_experience_gemini import ModelConnection, model
from test_gemini import FakeConnection, valid_response
from test_multiday_analysis import FakeControl

from project_log.analysis_checkpoints import (
    PREVIOUS_RETRYABLE,
    RETRY_COMPATIBILITY,
    compatibility_policy,
    contract_digest,
    resume_rows,
)
from project_log.analysis_child import ControlledModel
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_recovery import resume_block
from project_log.experience_gemini import GeminiExperienceModel, validate_request_schema
from project_log.experiences import Discovery
from project_log.gemini import GeminiConfig, _envelope_policy
from project_log.gemini_free_tier import STATE_LIMIT, FreeTierQuota
from project_log.git import CollectionError
from project_log.policy import sha256


@pytest.mark.parametrize("status", [500, 502, 503, 504, 401, 403, 404, 408, 413, 501])
def test_native_status_policy_one_shared_retry_budget_and_persistent_quota(tmp_path, status):
    connections = []

    def connection():
        c = ModelConnection(retries=0, verify=True, trust_env=False)
        c.status = status
        connections.append(c)
        return c

    control = FakeControl()
    quota = FreeTierQuota(tmp_path / "quota.json")
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        test_transport_factory=connection,
        before_send=control.enter,
        quota=quota,
    )
    with pytest.raises(CollectionError) as caught:
        ControlledModel(adapter, control, execution_profile("synthetic")).complete(
            "discover", "synthetic", (), "{}", Discovery
        )
    retry = status in {500, 502, 503, 504}
    expected = {
        401: "provider_authentication",
        403: "provider_permission_denied",
        404: "provider_model_unavailable",
        503: "provider_temporarily_unavailable",
    }.get(status, f"provider_server_error_h{status}" if retry else f"provider_http_h{status}")
    assert caught.value.code == expected
    assert (
        control.seq
        == len(connections)
        == json.loads(quota.path.read_text())["count"]
        == (2 if retry else 1)
    )
    assert control.waits == ([30] if retry else [])
    assert all(len(c.requests) == c.close_count == 1 for c in connections)
    assert len({c.body for c in connections}) == 1  # No input drift on a retry.
    assert adapter.usage == []


@pytest.mark.parametrize("failure", ["stream", "compressed", "oversize", "trailing", "type"])
def test_known_429_survives_unusable_diagnostics_without_retry_or_raw_data(tmp_path, failure):
    connection = FakeConnection(retries=0, verify=True, trust_env=False)
    connection.status = 429
    raw = json.dumps(
        {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinute"}],
                    }
                ]
            }
        }
    ).encode()
    connection.payload = raw
    if failure == "stream":
        connection.chunks = [raw]
        connection.stream_error = httpx.ReadTimeout("PRIVATE")
    elif failure == "compressed":
        connection.response_headers = {"content-encoding": "gzip"}
    elif failure == "oversize":
        connection.chunks = [raw, b" " * STATE_LIMIT, b"PRIVATE unread"]
    elif failure == "trailing":
        connection.payload += b"PRIVATE"
    else:
        connection.payload = raw.replace(b"type.googleapis.com/", b"PRIVATE/")
    connection.stream_close_error = RuntimeError("PRIVATE close")
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.5-flash-lite", "synthetic-key", True),
        test_transport_factory=lambda: connection,
        quota=FreeTierQuota(tmp_path / "quota.json"),
    )
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_rate_limited"
    assert "PRIVATE" not in str(caught.value)
    assert len(connection.requests) == connection.close_count == 1
    assert json.loads((tmp_path / "quota.json").read_text())["count"] == 1
    assert connection.stream.closed
    if failure == "oversize":
        assert connection.stream.yielded == 2


def test_structured_400_api_key_error_is_authentication_without_retaining_details():
    connection = FakeConnection(retries=0, verify=True, trust_env=False)
    connection.status = 400
    connection.payload = json.dumps(
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "PRIVATE",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "domain": "googleapis.com",
                        "reason": "API_KEY_INVALID",
                        "metadata": {"private": "PRIVATE"},
                    }
                ],
            }
        }
    ).encode()
    adapter = model()
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_authentication"
    assert "PRIVATE" not in str(caught.value)
    assert len(connection.requests) == 1


def test_tls_certificate_cause_is_configuration_not_ambiguous_network():
    connection = FakeConnection(retries=0, verify=True, trust_env=False)
    cert = ssl.SSLCertVerificationError("PRIVATE certificate")
    connection.error = httpx.ConnectError("PRIVATE")
    connection.error.__cause__ = cert
    adapter = model()
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_certificate_error"
    assert "PRIVATE" not in str(caught.value)
    assert len(connection.requests) == 1


def test_provable_schema_contract_violation_stops_before_transport_or_call_gate(monkeypatch):
    adapter = model()
    adapter.test_transport_factory = lambda: pytest.fail("No transport before validation")
    adapter.before_send = lambda _: pytest.fail("No call grant before validation")
    monkeypatch.setattr(
        Discovery,
        "model_json_schema",
        lambda: {
            "type": "object",
            "properties": {"seeds": {"$ref": "#/$defs/Seed", "description": "PRIVATE"}},
        },
    )
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (), "{}", Discovery)
    assert caught.value.code == "analysis_request_schema_invalid"
    assert "PRIVATE" not in str(caught.value)


def test_schema_validation_does_not_rewrite_or_invent_complexity_limits():
    schema = {
        "type": "object",
        "properties": {
            "$ref": {"type": "string"},
            "required": {"type": "string"},
            "x": {"$ref": "#/$defs/Value"},
        },
        "$defs": {"Value": {"type": "integer", "minimum": 0, "maximum": 1}},
        "required": ["$ref", "required", "x"],
    }
    before = canonical(schema)
    validate_request_schema(schema)
    assert canonical(schema) == before


@pytest.mark.parametrize(
    "raw",
    [
        valid_response().replace(b'"STOP"', b'"MAX_TOKENS", "finishReason":"STOP"'),
        valid_response().replace(b'"promptTokenCount": 10', b'"promptTokenCount": NaN'),
        valid_response().replace(
            b"The counter returns 0 instead of -1.", b'x\\",\\"observation\\":\\"y'
        ),
    ],
)
def test_ambiguous_json_is_rejected_before_permissive_sdk_parsing(raw):
    with pytest.raises(CollectionError) as caught:
        _envelope_policy(raw)
    assert caught.value.code == "provider_invalid_response"


@pytest.mark.parametrize(
    "code",
    [
        "invalid_unknown_claim",
        "unsupported_claim",
        "invalid_evidence_reference",
        "invalid_media_page",
        "invalid_evidence_quote",
        "invalid_discovery_reference_out_of_batch",
        "provider_blocked",
        "provider_incomplete",
        "provider_response_too_large",
        "analysis_request_schema_invalid",
        "analysis_request_too_large",
        "gemini_free_request_limit",
        "gemini_free_tokens_unknown",
    ],
)
def test_deterministic_response_and_request_failures_do_not_unlock_on_new_day(code):
    profile = execution_profile("synthetic")
    original = {
        "error_code": code,
        "consent": {"descriptor": {"profile": profile}},
        "finished_at": datetime.now(UTC),
    }
    expected = (
        "analysis_resume_request_fix_required"
        if code
        in {
            "analysis_request_schema_invalid",
            "analysis_request_too_large",
            "gemini_free_request_limit",
            "gemini_free_tokens_unknown",
        }
        else "analysis_resume_validation_required"
    )
    assert (
        resume_block(None, original, profile, now=original["finished_at"] + timedelta(days=2))
        == expected
    )


def test_retry_only_compatibility_preserves_responses_origins_and_400_guard():
    from test_experiences import evidence, inputs

    snapshot = inputs(evidence("one", "synthetic"))
    new = execution_profile("synthetic")
    old = {**new, "retryable": PREVIOUS_RETRYABLE}
    response = {"seeds": []}
    rows = [
        dict(
            ordinal=1,
            task="discover",
            request_digest="a" * 64,
            response=response,
            response_digest=sha256(canonical(response).encode()),
            contract_digest=contract_digest(snapshot.digest, old),
            origin_request_id="original",
            origin_call_seq=2,
        )
    ]
    copied = resume_rows(rows, snapshot, old, new)
    assert compatibility_policy(old, new) == RETRY_COMPATIBILITY
    assert {k: v for k, v in copied[0].items() if k != "contract_digest"} == {
        k: v for k, v in rows[0].items() if k != "contract_digest"
    }
    assert copied[0]["contract_digest"] == contract_digest(snapshot.digest, new)
    original = {
        "error_code": "provider_invalid_request",
        "consent": {"descriptor": {"profile": old}},
    }
    assert resume_block(None, original, new) == "analysis_resume_request_fix_required"
    for change in ({"model": "other"}, {"instruction_digest": "b" * 64}, {"retry_delay": 1}):
        with pytest.raises(CollectionError):
            resume_rows(rows, snapshot, {**old, **change}, new)


@pytest.mark.parametrize("delay", ["61s", "9" * 1000 + "s"])
def test_large_explicit_retry_hint_never_falls_back_to_a_shorter_wait(tmp_path, delay):
    from project_log.gemini_free_tier import MinuteProviderError

    q = FreeTierQuota(tmp_path / "quota.json")
    raw = json.dumps(
        {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinute"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                ]
            }
        }
    ).encode()
    response = httpx.Response(429, stream=httpx.ByteStream(raw))
    with pytest.raises(MinuteProviderError) as caught:
        q.reject_429(response)
    assert caught.value.delay > execution_profile("synthetic")["minute_retry_max_delay"]
    response.close()
