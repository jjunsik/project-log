import base64
import json

import httpx
import pytest
from test_experiences import evidence, inputs
from test_gemini import FakeConnection

from project_log.experience_gemini import GeminiExperienceModel, TransientProviderError
from project_log.experience_input import Evidence, Media
from project_log.experiences import (
    Chunk,
    Discovery,
    ExperienceGraph,
    Limits,
    batches,
    discovery_schema,
    split_evidence,
)
from project_log.gemini import HOST, GeminiConfig
from project_log.gemini_request import provider_schema
from project_log.git import CollectionError


class ModelConnection(FakeConnection):
    reference_violation = None
    response_override = None

    def handle_request(self, request):
        body = json.loads(request.read())
        prompt = json.loads(body["contents"][0]["parts"][0]["text"])
        task, refs = prompt["task"], prompt["evidence"]
        context = json.loads(prompt["context"])
        if task == "discover":
            response = {
                "seeds": [
                    {
                        "title": "counter 修正",
                        "rationale": "合成根拠",
                        "evidence_ids": [e["id"]],
                        "search_terms": ["counter"],
                    }
                    for e in refs
                ]
            }
        elif task == "consolidate":
            response = {"assignments": {seed["seed_id"]: 0 for seed in context}}
        elif task == "reconcile":
            response = {
                "assignments": {s["seed_id"]: 0 for s in context["seeds"]},
                "grounds": [
                    {
                        "label": 0,
                        "reason": "Synthetic event relation",
                        "support_ids": sorted(
                            {ref for s in context["seeds"] for ref in s["support_ids"]}
                        ),
                    }
                ],
                "splits": [],
            }
        elif task == "reconstruct":
            response = {
                "claims": [
                    {
                        "section": "implementation",
                        "statement": e["text"],
                        "status": "EVIDENCED",
                        "citations": [
                            {"evidence_id": e["id"], "quote": e["text"], "locator": None}
                        ],
                    }
                    for e in refs
                    if e["text"]
                ]
            }
        elif task == "audit":
            response = {
                "findings": [
                    {"claim_index": i, "supported": True, "explanation": "合成根拠一致"}
                    for i in range(len(context))
                ]
            }
        else:
            response = {"category": "strong", "rationale": "合成の境界条件改善"}
        if task == "discover" and self.reference_violation:
            ref = refs[0]["id"]
            response["seeds"][0]["evidence_ids"] = {
                "out_of_batch": ["synthetic-outside-id"],
                "duplicate": [ref, ref],
                "out_of_batch_duplicate": ["synthetic-outside-id", "synthetic-outside-id"],
            }[self.reference_violation]
        if self.response_override is not None:
            response = self.response_override
        self.payload = json.dumps(
            {
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(response)}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 10,
                    "totalTokenCount": 20,
                },
            }
        ).encode()
        return super().handle_request(request)


def model(model_id="gemini-3.1-flash-lite"):
    ModelConnection.calls = []
    adapter = GeminiExperienceModel(
        GeminiConfig(model_id, "synthetic-test-key", True),
        test_transport_factory=lambda: ModelConnection(retries=0, verify=True, trust_env=False),
    )
    return adapter


@pytest.mark.parametrize(
    "failure,kind",
    [
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.ReadError, "read_error"),
        (httpx.WriteError, "write_error"),
        (httpx.CloseError, "close_error"),
        (httpx.LocalProtocolError, "local_protocol"),
        (httpx.RemoteProtocolError, "remote_protocol"),
        (httpx.DecodingError, "decoding_error"),
        (httpx.StreamClosed, "stream_closed"),
        (httpx.ResponseNotRead, "response_not_read"),
        (httpx.StreamConsumed, "stream_consumed"),
        (httpx.HTTPError, "http_error"),
        (TimeoutError, "os_timeout"),
        (OSError, "os_error"),
        (RuntimeError, "unexpected_error"),
    ],
)
def test_sdk_failure_type_boundary_timeout_and_single_attempt_are_safe(failure, kind):
    adapter = model("gemini-3.5-flash-lite")
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    private = "synthetic-key private prompt header response evidence-id"
    connection.error = failure() if issubclass(failure, httpx.StreamError) else failure(private)
    connection.close_error = RuntimeError(private)
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    assert caught.value.code == f"provider_{kind}_transport_hunknown"
    assert private not in str(caught.value) and caught.value.__suppress_context__
    assert len(caught.value.code) <= 80
    assert len(connection.requests) == connection.close_count == 1
    assert connection.requests[0].extensions["timeout"] == {
        "connect": 120.0,
        "read": 120.0,
        "write": 120.0,
        "pool": 120.0,
    }


@pytest.mark.parametrize("partial", [False, True])
def test_body_timeout_preserves_known_status_and_primary_error_on_response_close(partial):
    adapter = model()
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.chunks = [b'{"candidates":'] if partial else []
    connection.stream_error = httpx.ReadTimeout("private body error")
    connection.stream_close_error = httpx.CloseError("private response close")
    connection.close_error = OSError("private transport close")
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    boundary = "body_partial" if partial else "body_wait"
    assert caught.value.code == f"provider_read_timeout_{boundary}_h200"
    assert "private" not in str(caught.value)
    assert connection.stream.closed and connection.close_count == len(connection.requests) == 1


@pytest.mark.parametrize(
    "kind", ["envelope", "structured", "sdk", "transport_close", "client_close"]
)
def test_response_validation_sdk_and_cleanup_boundaries_without_live_network(monkeypatch, kind):
    adapter = model()
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    adapter.test_transport_factory = lambda: connection
    if kind == "envelope":
        connection.chunks = [b"private invalid provider response"]
        expected = "provider_invalid_response_envelope_h200"
    elif kind == "structured":
        connection.response_override = {"seeds": "private invalid schema"}
        expected = "provider_validation_error_validation_h200"
    elif kind == "sdk":

        def broken_result(response):
            raise RuntimeError("private internal SDK failure")

        monkeypatch.setattr("langchain_google_genai.chat_models._response_to_result", broken_result)
        expected = "provider_unexpected_error_sdk_h200"
    elif kind == "transport_close":
        connection.close_error = httpx.CloseError("private close error")
        expected = "provider_close_error_transport_close_h200"
    else:

        def broken_close(client):
            raise RuntimeError("private SDK cleanup")

        monkeypatch.setattr("google.genai.Client.close", broken_close)
        expected = "provider_unexpected_error_client_close_h200"
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    assert caught.value.code == expected
    assert "private" not in str(caught.value) and len(caught.value.code) <= 80
    assert connection.close_count == len(connection.requests) == 1


def test_schema_rejection_preserves_response_and_validation_locations_without_live_http(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Live transport forbidden")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    adapter = model()
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.response_override = {"seeds": "Synthetic invalid schema"}
    adapter.test_transport_factory = lambda: connection
    recorded = []
    adapter.validation_failure = recorded.append
    with pytest.raises(CollectionError) as error:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    assert error.value.code == "provider_validation_error_validation_h200"
    assert len(connection.requests) == len(recorded) == 1
    diagnostic = recorded[0]
    assert diagnostic["condition"] == "structured_response_schema"
    assert diagnostic["http_status"] == 200
    assert diagnostic["provider_reason_status"] == "UNKNOWN"
    assert diagnostic["response"] == connection.response_override
    assert diagnostic["schema"] == Discovery.model_json_schema()
    assert diagnostic["errors"] == [{"type": "list_type", "loc": ["seeds"]}]


def test_sdk_initialization_failure_is_classified_before_transport_entry():
    adapter = model()

    def broken_factory():
        raise OSError("private initialization data")

    adapter.test_transport_factory = broken_factory
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_os_error_setup_hunknown"
    assert "private" not in str(caught.value)


def test_normal_response_does_not_fail_due_to_duplicate_transport_close():
    adapter = model()
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    original = connection.close

    def close_once():
        if connection.close_count:
            raise RuntimeError("private non-idempotent repeated close")
        original()

    connection.close = close_once
    adapter.test_transport_factory = lambda: connection
    assert adapter.complete("discover", "Synthetic", (), "{}", Discovery).seeds == []
    assert connection.close_count == 1


@pytest.mark.parametrize("boundary", ["request", "setup"])
def test_request_and_schema_failures_do_not_construct_a_transport(monkeypatch, boundary):
    adapter = model()
    chunks = split_evidence(inputs(evidence("one", "Synthetic evidence")), Limits())

    def fail(*args, **kwargs):
        raise RuntimeError("private request/schema processing")

    if boundary == "request":
        monkeypatch.setattr(Chunk, "payload", fail)
    else:
        monkeypatch.setattr(Discovery, "model_json_schema", fail)
    adapter.test_transport_factory = lambda: pytest.fail("Transport must not be constructed")
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "Synthetic", chunks, "{}", Discovery)
    assert caught.value.code == f"provider_unexpected_error_{boundary}_hunknown"


def test_explicit_503_is_preserved_when_response_and_transport_cleanup_fail():
    adapter = model()
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.status = 503
    connection.stream_close_error = RuntimeError("private close")
    connection.close_error = RuntimeError("private close")
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(TransientProviderError) as caught:
        adapter.complete("discover", "Synthetic", (), "{}", Discovery)
    assert caught.value.code == "provider_temporarily_unavailable"
    assert len(connection.requests) == connection.close_count == 1


def test_actual_sdk_transport_entry_gate_denies_before_synthetic_http():
    adapter = model()

    def deny(digest):
        assert len(digest) == 64
        raise CollectionError("analysis_stopped", "Synthetic cancelled entry")

    adapter.before_send = deny
    with pytest.raises(CollectionError) as exc:
        ExperienceGraph(adapter).run(inputs(evidence("one", "counter correction")))
    assert exc.value.code == "analysis_stopped"
    assert sum(len(c.requests) for c in ModelConnection.calls) == 0


def test_explicit_503_retry_classification_does_not_retry_in_sdk():
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.status = 503
    ModelConnection.calls = []
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.1-flash-lite", "synthetic-test-key", True),
        test_transport_factory=lambda: connection,
    )
    with pytest.raises(TransientProviderError):
        ExperienceGraph(adapter).run(inputs(evidence("one", "counter correction")))
    assert len(connection.requests) == 1


@pytest.mark.parametrize("model_id", ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
def test_real_langgraph_langchain_sdk_serialization_and_structured_parsing_without_network(
    monkeypatch,
    model_id,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP transport must not be constructed")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    adapter = model(model_id)
    result = ExperienceGraph(adapter).run(
        inputs(evidence("before", "counter returns -1"), evidence("after", "counter returns 0"))
    )
    assert result.candidates and result.candidates[0].review_status == "unreviewed"
    assert len(adapter.usage) == result.model_calls == len(ModelConnection.calls)
    for call in ModelConnection.calls:
        body = json.loads(call.body)
        assert call.closed and call.stream.closed and len(call.requests) == 1
        assert call.path == f"https://{HOST}/v1beta/models/{model_id}:generateContent"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in body["generationConfig"]
        assert "tools" not in body and "fileData" not in str(body)
        assert call.headers["x-goog-api-key"] == "synthetic-test-key"
        assert call.headers["accept-encoding"] == "identity"
        assert call.requests[0].extensions["timeout"] == {
            "connect": 120.0,
            "read": 120.0,
            "write": 120.0,
            "pool": 120.0,
        }


@pytest.mark.parametrize("model_id", ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
def test_discovery_sdk_uses_each_batch_schema_without_leaking_other_batch_ids(
    monkeypatch, model_id
):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP transport must not be constructed")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    limits = Limits(chunk_chars=40, batch_chars=300)
    snapshot = inputs(evidence("one", "first counter"), evidence("two", "second counter"))
    expected = batches(split_evidence(snapshot, limits), limits)
    adapter = model(model_id)
    result = ExperienceGraph(adapter, limits).run(snapshot)
    discovery_calls = [
        json.loads(c.body)
        for c in ModelConnection.calls
        if json.loads(json.loads(c.body)["contents"][0]["parts"][0]["text"])["task"] == "discover"
    ]
    assert len(discovery_calls) == len(expected) == 2
    for call, batch in zip(discovery_calls, expected, strict=True):
        schema = call["generationConfig"]["responseJsonSchema"]
        assert schema == provider_schema(discovery_schema(batch).model_json_schema())
        assert schema["properties"]["seeds"]["items"]["properties"]["evidence_ids"]["items"] == {
            "type": "string",
            "enum": [c.id for c in batch],
        }
    assert result.candidates[0].evidence_ids == ["one", "two"]


@pytest.mark.parametrize("kind", ["out_of_batch", "duplicate", "out_of_batch_duplicate"])
def test_sdk_reference_violations_reach_domain_diagnostics_without_retry_or_raw_output(
    monkeypatch, capsys, caplog, kind
):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP transport must not be constructed")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    adapter = model("gemini-3.5-flash-lite")
    connection = ModelConnection(retries=0, verify=True, trust_env=False)
    connection.reference_violation = kind
    adapter.test_transport_factory = lambda: connection
    with pytest.raises(CollectionError) as error:
        ExperienceGraph(adapter).run(inputs(evidence("one", "synthetic confidential body")))
    assert error.value.code == f"invalid_discovery_reference_{kind}"
    assert len(connection.requests) == 1 and len(adapter.usage) == 1
    assert str(error.value) == "발견 후보의 근거 참조가 유효하지 않습니다."
    captured = capsys.readouterr()
    assert "synthetic confidential body" not in captured.out + captured.err + caplog.text
    assert "synthetic-outside-id" not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("model_id", ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
@pytest.mark.parametrize(
    "mime,raw",
    [
        ("image/png", b"\x89PNG\r\n\x1a\nfixture"),
        ("image/jpeg", b"\xff\xd8\xfffixture\xff\xd9"),
        ("application/pdf", b"%PDF-1.7\nfixture\n%%EOF"),
    ],
)
def test_native_media_is_transmitted_exactly_after_manual_review(model_id, mime, raw):
    adapter = model(model_id)
    chunks = split_evidence(
        inputs(Evidence("media", "{}", media=(Media("fixture", mime, raw),))), Limits()
    )
    adapter.complete("discover", "Synthetic instruction", chunks, "{}", Discovery)
    body = json.loads(ModelConnection.calls[0].body)
    assert body["contents"][0]["parts"][1] == {
        "inlineData": {"mime_type": mime, "data": base64.urlsafe_b64encode(raw).decode()}
    }


@pytest.mark.parametrize("model_id", ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
def test_no_factory_or_synthetic_consent_can_enable_project_transfer(monkeypatch, model_id):
    def forbidden(*args, **kwargs):
        pytest.fail("Model construction before disabled gate")

    monkeypatch.setattr("project_log.experience_gemini.ChatGoogleGenerativeAI", forbidden)
    adapter = GeminiExperienceModel(GeminiConfig(model_id, "synthetic-test-key", True))
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "instruction", (), "{}", Discovery)
    assert exc.value.code == "analysis_transfer_disabled"


def test_preparation_only_blocks_adapter_even_with_injected_transport(monkeypatch):
    monkeypatch.setenv("PROJECT_LOG_AI_PREPARATION_ONLY", "1")
    adapter = model("gemini-3.5-flash-lite")
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "instruction", (), "{}", Discovery)
    assert exc.value.code == "analysis_transfer_disabled"
    assert ModelConnection.calls == []


def test_sdk_wire_preserves_text_context_paths_and_secret_shaped_synthetic_data():
    adapter = model("gemini-3.5-flash-lite")
    path = "/Users/synthetic-private-account/repo/counter.py"
    source = {"path_b64": base64.b64encode(path.encode()).decode(), "filename": path}
    text = "api_key=syntheticsecretvalue; account " + path
    chunk = Chunk("synthetic-id", ("original-reference",), json.dumps([source]), text)
    original = chunk.payload()
    context = json.dumps({"note": path})
    adapter.complete("discover", "Review " + path, (chunk,), context, Discovery)
    body = json.loads(ModelConnection.calls[0].body)
    prompt = json.loads(body["contents"][0]["parts"][0]["text"])
    assert prompt == {"task": "discover", "context": context, "evidence": [original]}
    assert body["systemInstruction"]["parts"] == [{"text": "Review " + path}]
    assert chunk.payload() == original


def test_sdk_graph_keeps_original_quotes_and_references_without_output_masking():
    adapter = model("gemini-3.5-flash-lite")
    text = "counter at /Users/synthetic-private-account/repo api_key=syntheticsecretvalue"
    original = inputs(evidence("original-reference", text))
    result = ExperienceGraph(adapter).run(original)
    claims = [claim for c in result.candidates for claim in c.claims if claim.status == "EVIDENCED"]
    assert claims and all(c.statement == text for c in claims)
    assert result.candidates[0].evidence_ids == ["original-reference"]
    assert original.entries[0].text == text
    assert result.coverage["offset_basis"] == "prepared_input"


@pytest.mark.parametrize(
    "surface",
    [
        "text",
        "system",
        "system_role",
        "extra_part",
        "schema",
        "extra_field",
        "candidate_count",
        "media_bytes",
    ],
)
def test_actual_sdk_wire_rejects_changes_before_call_entry(monkeypatch, surface):
    from project_log.experience_gemini import _ExperienceTransport

    original = _ExperienceTransport.handle_request

    def corrupt(self, request):
        body = json.loads(request.content)
        if surface == "text":
            body["contents"][0]["parts"][0]["text"] += "synthetic unexpected"
        elif surface == "system":
            body["systemInstruction"]["parts"][0]["text"] += "synthetic unexpected"
        elif surface == "extra_part":
            body["contents"][0]["parts"].append({"text": "synthetic unexpected"})
        elif surface == "schema":
            body["generationConfig"]["responseJsonSchema"]["description"] = "synthetic unexpected"
        elif surface == "system_role":
            body["systemInstruction"]["role"] = "synthetic unexpected"
        elif surface == "candidate_count":
            body["generationConfig"]["candidateCount"] = 2
        elif surface == "media_bytes":
            body["contents"][0]["parts"][1]["inlineData"]["data"] = "YWJj"
        else:
            body["cachedContent"] = "synthetic unexpected"
        changed = httpx.Request(request.method, request.url, headers=request.headers, json=body)
        return original(self, changed)

    monkeypatch.setattr(_ExperienceTransport, "handle_request", corrupt)
    adapter = model("gemini-3.5-flash-lite")
    entered = []
    adapter.before_send = entered.append
    chunks = (
        (Chunk("native", (), "[]", None, Media("synthetic", "image/png", b"original")),)
        if surface == "media_bytes"
        else ()
    )
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "Synthetic instruction", chunks, "{}", Discovery)
    assert exc.value.code == "analysis_wire_mismatch"
    assert not entered and sum(len(c.requests) for c in ModelConnection.calls) == 0
