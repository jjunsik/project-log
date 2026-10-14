import json
import ssl
from pathlib import Path

import httpx
import pytest

from project_log.gemini import (
    HOST,
    MAX_RESPONSE_BYTES,
    POLICY_VERSION,
    GeminiClient,
    GeminiConfig,
    SyntheticConsent,
    read_operator_key,
    synthetic_digest,
)
from project_log.git import CollectionError


def valid_response(**overrides):
    response = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "observation": "The counter returns 0 instead of -1.",
                                    "evidence_ids": ["synthetic/before", "synthetic/after"],
                                }
                            )
                        }
                    ]
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 20},
    }
    response.update(overrides)
    return json.dumps(response).encode()


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def close(self):
        self.closed = True


class FakeConnection(httpx.BaseTransport):
    status = 200
    payload = valid_response()
    error = None
    calls = []
    chunks = None
    response_headers = {}

    def __init__(self, **kwargs):
        assert kwargs == {"retries": 0, "verify": True, "trust_env": False}
        self.closed = False
        self.requests = []
        self.calls.append(self)

    def handle_request(self, request):
        self.requests.append(request)
        self.method, self.path, self.body, self.headers = (
            request.method,
            str(request.url),
            request.read(),
            request.headers,
        )
        if self.error:
            raise self.error
        self.stream = TrackedStream(self.chunks if self.chunks is not None else [self.payload])
        return httpx.Response(self.status, headers=self.response_headers, stream=self.stream)

    def close(self):
        self.closed = True


@pytest.fixture
def connection(monkeypatch):
    FakeConnection.calls = []
    monkeypatch.setattr("project_log.gemini.httpx.HTTPTransport", FakeConnection)
    return FakeConnection


def client_and_consent():
    client = GeminiClient(GeminiConfig("gemini-3.1-flash-lite", "synthetic-test-key", True))
    consent = SyntheticConsent(client.config.model, synthetic_digest(), POLICY_VERSION, True)
    return client, consent


def test_fixed_synthetic_payload_header_key_bounded_response_and_closed_connection(connection):
    client, consent = client_and_consent()
    result = client.verify_synthetic(consent)
    assert result.evidence_ids == ("synthetic/before", "synthetic/after")
    assert json.loads(result.usage_json) == {"promptTokenCount": 10, "totalTokenCount": 20}
    call = connection.calls[0]
    assert call.method == "POST" and call.closed
    assert "synthetic-test-key" not in call.path and "synthetic-test-key" not in repr(client.config)
    assert call.headers["x-goog-api-key"] == "synthetic-test-key"
    assert call.headers["accept-encoding"] == "identity"
    assert call.path == f"https://{HOST}/v1beta/models/{client.config.model}:generateContent"
    assert len(call.requests) == 1 and call.stream.closed
    assert call.requests[0].extensions["timeout"]["read"] == 30
    payload = json.loads(call.body)
    assert not payload.get("tools") and "fileData" not in str(payload)
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" in payload["generationConfig"]
    assert payload["generationConfig"]["maxOutputTokens"] == 512
    assert "synthetic/before" in str(payload) and "synthetic/after" in str(payload)
    assert not hasattr(client, "analyze")


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("model", "other"),
        ("input_sha256", "stale"),
        ("policy_version", "stale"),
    ],
)
def test_consent_binds_input_model_and_policy_before_any_connection(connection, field, value):
    client, consent = client_and_consent()
    from dataclasses import replace

    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(replace(consent, **{field: value}))
    assert exc.value.code == "transfer_not_confirmed" and connection.calls == []


def test_default_disabled_and_no_unreviewed_or_paid_fallback(connection, monkeypatch):
    monkeypatch.delenv("PROJECT_LOG_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("PROJECT_LOG_GEMINI_KEY_FILE", raising=False)
    with pytest.raises(CollectionError) as exc:
        GeminiConfig.from_environment()
    assert exc.value.code == "ai_not_configured"
    for config in (
        GeminiConfig("gemini-3.1-flash-lite", "synthetic-test-key"),
        GeminiConfig("unreviewed-model", "synthetic-test-key", True),
    ):
        with pytest.raises(CollectionError):
            GeminiClient(config).verify_synthetic(SyntheticConsent(config.model, "", "", True))
    assert connection.calls == []


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "provider_invalid_request"),
        (401, "provider_authentication"),
        (403, "provider_permission_denied"),
        (404, "provider_model_unavailable"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (302, "provider_unavailable"),
    ],
)
def test_http_errors_sanitized_without_redirect_retry_or_error_body(
    connection, monkeypatch, status, code
):
    monkeypatch.setattr(connection, "status", status)
    monkeypatch.setattr(
        connection, "payload", b"private provider error with key synthetic-test-key"
    )
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == code and "synthetic-test-key" not in str(exc.value)
    assert len(connection.calls) == 1 and connection.calls[0].closed
    assert len(connection.calls[0].requests) == 1


def test_network_failure_and_oversized_response(connection, monkeypatch):
    client, consent = client_and_consent()
    monkeypatch.setattr(connection, "error", TimeoutError("private error details"))
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_network_error" and "private" not in str(exc.value)
    assert connection.calls[-1].closed
    monkeypatch.setattr(connection, "error", ssl.SSLCertVerificationError("private TLS details"))
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_certificate_error" and "private" not in str(exc.value)
    assert connection.calls[-1].closed
    monkeypatch.setattr(connection, "error", None)
    monkeypatch.setattr(connection, "payload", b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_response_too_large"


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"not json", "provider_invalid_response"),
        (b"[]", "provider_invalid_response"),
        (valid_response(candidates=[]), "provider_invalid_response"),
        (valid_response(promptFeedback={"blockReason": "SAFETY"}), "provider_blocked"),
        (valid_response(candidates=[{"finishReason": "MAX_TOKENS"}]), "provider_incomplete"),
        (valid_response(usageMetadata={"promptTokenCount": True}), "provider_invalid_response"),
        (
            valid_response().replace(b"synthetic/after", b"unproven/claim"),
            "provider_invalid_response",
        ),
        (valid_response().replace(b"observation", b"unexpected"), "provider_invalid_response"),
    ],
)
def test_blocked_incomplete_malformed_and_unsupported_evidence(
    connection, monkeypatch, payload, code
):
    monkeypatch.setattr(connection, "payload", payload)
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == code
    assert len(connection.calls[0].requests) == 1


def test_operator_key_permissions_symlink_and_safe_environment(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key\n")
    key.chmod(0o600)
    assert read_operator_key(key) == "synthetic-test-key"
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(key))
    assert GeminiConfig.from_environment(free_tier_confirmed=True).api_key == "synthetic-test-key"
    key.write_text("AQ.synthetic-test-key\n")
    assert read_operator_key(key) == "AQ.synthetic-test-key"
    assert (
        GeminiConfig.from_environment(free_tier_confirmed=True).api_key == "AQ.synthetic-test-key"
    )
    key.chmod(0o644)
    with pytest.raises(CollectionError):
        read_operator_key(key)
    key.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(CollectionError):
        read_operator_key(link)
    for content in ("", "key\nheader-injection", "x" * 257):
        key.write_text(content)
        with pytest.raises(CollectionError):
            read_operator_key(key)
    with pytest.raises(CollectionError):
        read_operator_key(Path("/does-not-exist"))


def test_optional_thoughts_are_not_treated_as_evidence(connection, monkeypatch):
    response = json.loads(valid_response())
    parts = response["candidates"][0]["content"]["parts"]
    parts[0]["thoughtSignature"] = "opaque-provider-signature"
    parts.insert(0, {"text": "internal reasoning is not evidence", "thought": True})
    monkeypatch.setattr(connection, "payload", json.dumps(response).encode())
    client, consent = client_and_consent()
    result = client.verify_synthetic(consent)
    assert "internal reasoning" not in result.observation
    assert result.evidence_ids == ("synthetic/before", "synthetic/after")


def test_cli_default_does_not_read_key_or_call_provider(connection, monkeypatch, capsys):
    from scripts.verify_gemini import main

    monkeypatch.setattr("sys.argv", ["verify_gemini"])
    monkeypatch.setattr(
        "project_log.gemini.read_operator_key",
        lambda _: pytest.fail("must not read credential before confirmation"),
    )
    assert main() == 2
    assert connection.calls == []
    assert "외부 호출하지 않음" in capsys.readouterr().out


def test_oversized_stream_is_closed_before_reading_remainder_or_sdk_parsing(
    connection, monkeypatch
):
    monkeypatch.setattr(connection, "chunks", [b"x" * MAX_RESPONSE_BYTES, b"x", b"never read"])
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_response_too_large"
    call = connection.calls[0]
    assert call.stream.yielded == 2 and call.stream.closed
    assert len(call.requests) == 1 and call.closed


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"content-length": str(MAX_RESPONSE_BYTES + 1)}, "provider_response_too_large"),
        ({"content-encoding": "gzip"}, "provider_invalid_response"),
    ],
)
def test_oversize_header_and_compression_fail_before_any_body_read(
    connection, monkeypatch, headers, code
):
    monkeypatch.setattr(connection, "response_headers", headers)
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == code
    assert connection.calls[0].stream.yielded == 0
    assert connection.calls[0].stream.closed


def test_exact_byte_limit_is_accepted(connection, monkeypatch):
    payload = valid_response()
    monkeypatch.setattr(connection, "chunks", [payload, b" " * (MAX_RESPONSE_BYTES - len(payload))])
    client, consent = client_and_consent()
    assert client.verify_synthetic(consent).evidence_ids == ("synthetic/before", "synthetic/after")


def test_transport_rejects_second_request_and_other_endpoints(connection):
    from project_log.gemini import _SyntheticTransport

    transport = _SyntheticTransport("gemini-3.1-flash-lite")
    for url in (
        "https://example.invalid/",
        "https://generativelanguage.googleapis.com/v1beta/files",
    ):
        with pytest.raises(CollectionError):
            transport.handle_request(httpx.Request("POST", url))
    request = httpx.Request("POST", transport.endpoint)
    transport.handle_request(request).close()
    with pytest.raises(CollectionError) as exc:
        transport.handle_request(request)
    assert exc.value.code == "provider_request_blocked"
    assert len(connection.calls[0].requests) == 1
    transport.close()


@pytest.mark.parametrize("name", ["GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE"])
def test_ambient_paid_backend_is_blocked_before_client_creation(connection, monkeypatch, name):
    monkeypatch.setenv(name, "true")
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "ai_not_configured" and connection.calls == []


def test_ambient_tracing_does_not_create_remote_tracer(connection, monkeypatch):
    import langchain_core.tracers.context as context

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(context, "LangChainTracer", lambda **_: pytest.fail("remote tracing"))
    client, consent = client_and_consent()
    assert client.verify_synthetic(consent).evidence_ids


def test_sdk_transient_httpx_error_is_not_retried(connection, monkeypatch):
    monkeypatch.setattr(connection, "error", httpx.ConnectError("private network details"))
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_network_error"
    assert len(connection.calls[0].requests) == 1


def test_wrapped_certificate_failure_preserves_safe_error_code(connection, monkeypatch):
    failure = httpx.ConnectError("private TLS details")
    failure.__cause__ = ssl.SSLCertVerificationError("private certificate details")
    monkeypatch.setattr(connection, "error", failure)
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_certificate_error"
    assert "private" not in str(exc.value) and exc.value.__suppress_context__
    assert len(connection.calls[0].requests) == 1


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"observation":"example","evidence_ids":["synthetic/before"]}\n```',
        '{"observation":"example","evidence_ids":["synthetic/before"]',
        '{"observation":" ","evidence_ids":["synthetic/before"]}',
        '{"observation":"example","evidence_ids":["synthetic/before","synthetic/before"]}',
        '{"observation":"example","evidence_ids":[1]}',
        '{"observation":"example","evidence_ids":[]}',
        '{"observation":"example","evidence_ids":["synthetic/before"],"extra":"value"}',
    ],
)
def test_native_parser_cannot_relax_original_json_and_evidence_contract(
    connection, monkeypatch, text
):
    response = json.loads(valid_response())
    response["candidates"][0]["content"]["parts"] = [{"text": text}]
    monkeypatch.setattr(connection, "payload", json.dumps(response).encode())
    client, consent = client_and_consent()
    with pytest.raises(CollectionError) as exc:
        client.verify_synthetic(consent)
    assert exc.value.code == "provider_invalid_response"
    assert len(connection.calls[0].requests) == 1
