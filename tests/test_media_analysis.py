import json
from dataclasses import replace

import httpx
import pytest
from media_fixtures import PNG, pdf, png_chunk
from test_experience_gemini import ModelConnection, model
from test_experiences import OfflineModel, evidence, inputs
from test_materials import project

from project_log.analysis_input import canonical
from project_log.document_media import MAX_INLINE_REQUEST_BYTES
from project_log.experience_input import Evidence, Media, material_evidence
from project_log.experience_store import Experiences
from project_log.experiences import (
    Citation,
    Claim,
    Discovery,
    ExperienceGraph,
    Limits,
    Reconstruction,
    split_evidence,
)
from project_log.git import CollectionError
from project_log.policy import MAX_BODY, SECRET, has_secret, sha256


class PageModel(OfflineModel):
    def complete(self, task, instruction, chunks, context, schema):
        if task != "reconstruct":
            return super().complete(task, instruction, chunks, context, schema)
        return Reconstruction(
            claims=[
                Claim(
                    section="implementation",
                    statement="Synthetic page interpretation",
                    status="INFERRED",
                    citations=[
                        Citation(
                            evidence_id=c.id,
                            quote=None,
                            locator="synthetic marker",
                            page=json.loads(c.media.provenance_json)["page_start"],
                        )
                    ],
                )
                for c in chunks
                if c.media
            ]
            + [
                Claim(
                    section="implementation",
                    statement=c.text,
                    status="EVIDENCED",
                    citations=[Citation(evidence_id=c.id, quote=c.text, locator=None)],
                )
                for c in chunks
                if c.text
            ]
        )


def test_pdf_pages_reconstruct_across_collections_and_survive_storage(db, tmp_path):
    pid = project(db, tmp_path)
    raw = pdf(70)
    prepared = material_evidence(
        "before/pdf",
        {
            "filename": "counter.pdf",
            "collection_id": "collection-before",
            "kind": "historical_source",
            "commit_oid": "before",
        },
        raw,
    )
    snapshot = replace(
        inputs(prepared, evidence("after", "counter correction", cid="collection-after")),
        project_id=pid,
    )
    result = ExperienceGraph(PageModel()).run(snapshot)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert set(candidate.evidence_ids) == {"before/pdf", "after"}
    citations = [c for claim in candidate.claims for c in claim.citations if c.page]
    assert {c.page for c in citations} == {1, 32, 63}
    assert all(
        claim.status == "INFERRED"
        for claim in candidate.claims
        if any(c.page for c in claim.citations)
    )
    saved = Experiences(db).save(snapshot, result, model="offline-page-fixture")
    store = Experiences(db)
    copy = store.evidence(pid, str(saved["id"]), "before/pdf")
    assert copy["media"][0]["use_for_analysis"] is False
    assert store.media(pid, str(saved["id"]), "before/pdf", 0)[1] == raw
    for index, media in enumerate(prepared.media):
        assert copy["media"][index]["provenance"] == json.loads(media.provenance_json)
        assert store.media(pid, str(saved["id"]), "before/pdf", index)[1] == media.raw
    assert saved["result"]["coverage"] == result.coverage


def test_pdf_citation_requires_global_page_in_provided_range():
    media = Media(
        "part.pdf", "application/pdf", b"synthetic", canonical({"page_start": 32, "page_end": 63})
    )
    chunks = split_evidence(inputs(Evidence("pdf", "{}", media=(media,))), Limits())
    for page in (None, 1, 64):
        claim = Claim(
            section="result",
            statement="Synthetic",
            status="INFERRED",
            citations=[Citation(evidence_id=chunks[0].id, quote=None, locator="table", page=page)],
        )
        with pytest.raises(CollectionError, match="PDF") as caught:
            ExperienceGraph._validate_claims([claim], chunks)
        assert caught.value.code == "invalid_media_page"
    claim.citations[0].page = 32
    ExperienceGraph._validate_claims([claim], chunks)


@pytest.mark.parametrize("mime", ["image/png", "application/pdf"])
def test_default_upload_sized_media_survives_real_sdk_inline_serialization(mime, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP construction is forbidden")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    raw = (
        PNG[:-12]
        + png_chunk(b"tEXt", b"Padding\0" + b"x" * (20 * MAX_BODY - len(PNG) - 20))
        + PNG[-12:]
        if mime == "image/png"
        else pdf(padding=20 * MAX_BODY - 4096)
    )
    assert 19 * MAX_BODY < len(raw) <= 20 * MAX_BODY
    adapter = model()
    entry = material_evidence(
        "large", {"filename": "large.png" if mime == "image/png" else "large.pdf"}, raw
    )
    assert entry.reason is None
    chunks = split_evidence(inputs(entry), Limits())
    adapter.complete("discover", "Synthetic instructions", chunks, "{}", Discovery)
    wire = ModelConnection.calls[0].body
    assert 19 * MAX_BODY < len(wire) <= MAX_INLINE_REQUEST_BYTES
    import base64

    body = json.loads(wire)
    part = next(p["inlineData"] for p in body["contents"][0]["parts"] if "inlineData" in p)
    # google-genai's real JSON serializer uses URL-safe base64 for bytes.
    assert sha256(base64.urlsafe_b64decode(part["data"])) == sha256(raw)


def test_wire_size_includes_system_and_schema_before_transport_call(monkeypatch):
    # Content alone fits; the final SDK envelope exceeds the deliberately small budget.
    monkeypatch.setattr("project_log.experience_gemini.MAX_INLINE_REQUEST_BYTES", 2500)
    adapter = model()
    chunks = split_evidence(inputs(evidence("one", "synthetic")), Limits())
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic instruction " * 500, chunks, "{}", Discovery)
    assert caught.value.code == "analysis_request_too_large"
    assert ModelConnection.calls and all(not c.requests and c.closed for c in ModelConnection.calls)


def test_original_docx_copy_cannot_be_used_as_model_input():
    from project_log.experiences import Chunk

    adapter = model()
    chunk = Chunk(
        "original",
        ("docx",),
        "[]",
        None,
        Media(
            "original.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"synthetic",
            use_for_analysis=False,
        ),
    )
    with pytest.raises(CollectionError) as caught:
        adapter.complete("discover", "synthetic", (chunk,), "{}", Discovery)
    assert caught.value.code == "analysis_media_unavailable"
    assert ModelConnection.calls == []


def test_large_input_secret_search_preserves_legacy_detection_rules():
    values = [
        b"api_key=syntheticsecretvalue",
        b"password ' : \"syntheticvalue",
        b"BEARER " + b"x" * 20,
        b"https://user:password@host",
        b"plain",
        b"x" * 4096,
        b"secret" + b" " * 4096,
    ]
    for lead in range(256):
        for scheme in (b"https", b"0x", b"0", b"x_", b"x+.9", b"", b"...A9"):
            values.append(bytes([lead]) + scheme + b"://user:pass@host")
    for raw in values:
        assert has_secret(raw) == bool(SECRET.search(raw))
    assert not has_secret(b"x" * (20 * MAX_BODY))
    assert has_secret(b"x" * (20 * MAX_BODY) + b"://user:pass@host")


def test_docx_derivative_mapping_and_failure_are_not_confused_with_real_rendering(monkeypatch):
    from media_fixtures import visual_docx

    raw = visual_docx()
    rendered = pdf(2)
    monkeypatch.setattr(
        "project_log.experience_input.render_docx",
        lambda body: (
            rendered,
            {"transform": "docx-to-pdf", "renderer_version": "synthetic-test"},
            (),
        ),
    )
    prepared = material_evidence("docx", {"filename": "visual.docx"}, raw)
    assert "AFTER 0" in prepared.text
    original = next(m for m in prepared.media if m.mime_type.endswith("wordprocessingml.document"))
    assert original.raw == raw and not original.use_for_analysis
    part = next(m for m in prepared.media if m.mime_type == "application/pdf")
    info = json.loads(part.provenance_json)
    assert (info["page_start"], info["page_end"]) == (1, 2)
    assert info["original_sha256"] == sha256(raw)
    assert "docx_rendering_fidelity_unverified" in prepared.limitations

    def failed(body):
        raise CollectionError("media_processing_failed", "synthetic failure")

    monkeypatch.setattr("project_log.experience_input.render_docx", failed)
    partial = material_evidence("docx", {"filename": "visual.docx"}, raw)
    assert partial.reason is None and partial.text == prepared.text
    assert any(m.raw == raw and not m.use_for_analysis for m in partial.media)
    assert not any(m.mime_type == "application/pdf" for m in partial.media)
    assert "docx_visual_unprocessed:media_processing_failed" in partial.limitations
