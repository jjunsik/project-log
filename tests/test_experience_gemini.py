import base64
import json

import httpx
import pytest
from test_experiences import evidence, inputs
from test_gemini import FakeConnection

from project_log.experience_gemini import GeminiExperienceModel
from project_log.experience_input import Evidence, Media
from project_log.experiences import Discovery, ExperienceGraph, Limits, split_evidence
from project_log.gemini import GeminiConfig
from project_log.git import CollectionError


class ModelConnection(FakeConnection):
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
            response = {"groups": [{"seed_ids": list(range(len(context)))}]}
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


def model():
    ModelConnection.calls = []
    return GeminiExperienceModel(
        GeminiConfig("gemini-3.1-flash-lite", "synthetic-test-key", True),
        test_transport_factory=lambda: ModelConnection(retries=0, verify=True, trust_env=False),
    )


def test_real_langgraph_langchain_sdk_serialization_and_structured_parsing_without_network(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP transport must not be constructed")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    adapter = model()
    result = ExperienceGraph(adapter).run(
        inputs(evidence("before", "counter returns -1"), evidence("after", "counter returns 0"))
    )
    assert result.candidates and result.candidates[0].review_status == "unreviewed"
    assert len(adapter.usage) == result.model_calls == len(ModelConnection.calls)
    for call in ModelConnection.calls:
        body = json.loads(call.body)
        assert call.closed and call.stream.closed and len(call.requests) == 1
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in body["generationConfig"]
        assert "tools" not in body and "fileData" not in str(body)
        assert call.headers["x-goog-api-key"] == "synthetic-test-key"
        assert call.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "mime,raw",
    [("image/png", b"\x89PNG\r\n\x1a\nfixture"), ("application/pdf", b"%PDF-1.7\nfixture\n%%EOF")],
)
def test_native_media_uses_inline_bytes_not_file_upload_or_url(mime, raw):
    adapter = model()
    chunks = split_evidence(
        inputs(Evidence("media", "{}", media=(Media("fixture", mime, raw),))), Limits()
    )
    result = adapter.complete("discover", "Synthetic instruction", chunks, "{}", Discovery)
    assert result.seeds[0].evidence_ids == [chunks[0].id]
    request = json.loads(ModelConnection.calls[0].body)
    part = next(p for p in request["contents"][0]["parts"] if "inlineData" in p)
    assert part["inlineData"]["mime_type"] == mime
    assert base64.b64decode(part["inlineData"]["data"]) == raw
    assert len(ModelConnection.calls) == 1


def test_no_factory_or_synthetic_consent_can_enable_project_transfer(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Model construction before disabled gate")

    monkeypatch.setattr("project_log.experience_gemini.ChatGoogleGenerativeAI", forbidden)
    adapter = GeminiExperienceModel(
        GeminiConfig("gemini-3.1-flash-lite", "synthetic-test-key", True)
    )
    with pytest.raises(CollectionError) as exc:
        adapter.complete("discover", "instruction", (), "{}", Discovery)
    assert exc.value.code == "analysis_transfer_disabled"
