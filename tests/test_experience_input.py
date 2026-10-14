import json
from dataclasses import replace

import pytest
from media_fixtures import pdf
from test_analysis_input import docx, word

from project_log.analysis_input import AnalysisInput
from project_log.collector import collect
from project_log.experience_input import (
    MAX_MEDIA_BYTES,
    ExperienceInputs,
    material_evidence,
)
from project_log.experiences import Limits, split_evidence
from project_log.materials import Materials
from project_log.policy import MAX_BODY
from project_log.service import Projects, Registration


def material(filename, raw):
    return material_evidence(
        "synthetic/material", {"kind": "user_material", "filename": filename}, raw
    )


@pytest.mark.parametrize("ext", ["txt", "md", "log", "json", "csv"])
def test_all_text_upload_formats_and_large_body_chunking(ext):
    raw = b"counter " * (MAX_BODY // 8 + 1)
    result = material("sample." + ext, raw)
    assert result.text.encode() == raw and result.reason is None
    assert material("sample." + ext, b"api_key=syntheticsecretvalue").reason == "suspected_secret"
    assert material("sample." + ext, b"x\0y").reason == "invalid_format"


@pytest.mark.parametrize(
    "ext,raw,mime",
    [
        ("pdf", pdf(), "application/pdf"),
        ("png", b"\x89PNG\r\n\x1a\nsynthetic", "image/png"),
        ("jpg", b"\xff\xd8\xffsynthetic", "image/jpeg"),
        ("jpeg", b"\xff\xd8\xffsynthetic", "image/jpeg"),
    ],
)
def test_native_media_preparation_is_not_content_verification(ext, raw, mime):
    result = material("sample." + ext, raw)
    assert result.reason is None and result.text is None
    assert len(result.media) == 1 and result.media[0].raw == raw
    assert result.media[0].mime_type == mime
    assert "native_media_requires_visual_privacy_review" in result.limitations
    assert "native_media_content_not_locally_verified" in result.limitations
    assert material("credentials." + ext, raw).reason == "sensitive_path"
    assert (
        material("sample." + ext, raw + b" api_key=syntheticsecretvalue").reason
        == "suspected_secret"
    )
    assert material("sample." + ext, b"invalid").reason == "invalid_format"


def test_docx_text_images_and_unsupported_components_remain_visible():
    image = b"\x89PNG\r\n\x1a\nsynthetic"
    raw = docx(
        word("counter correction"),
        {
            "word/media/example.png": image,
            "word/media/chart.svg": b"<svg/>",
            "word/embeddings/table.bin": b"fixture",
        },
    )
    result = material("sample.docx", raw)
    assert result.text == "counter correction" and result.media[0].raw == image
    assert "docx_unsupported_media:word/media/chart.svg" in result.limitations
    assert "docx_embedded_attachments_unprocessed" in result.limitations
    assert "docx_images_or_attachments_unprocessed" not in result.limitations
    assert "docx_image_layout_and_text_association_not_preserved" in result.limitations
    image_only = material("sample.docx", docx(word(""), {"word/media/example.png": image}))
    assert image_only.reason is None and image_only.media
    tracked = word("counter").replace("<w:r>", "<w:ins><w:r>").replace("</w:r>", "</w:r></w:ins>")
    assert (
        material("sample.docx", docx(tracked, {"word/media/example.png": image})).reason
        == "docx_tracked_changes_requires_review"
    )
    assert (
        material("sample.docx", docx(word("api_key=syntheticsecretvalue"))).reason
        == "suspected_secret"
    )


def test_media_size_and_docx_decompression_boundaries():
    assert material("sample.pdf", b"%PDF-" + b"x" * MAX_MEDIA_BYTES).reason == "media_size_limit"
    zipped = docx(word("counter"), {"word/media/example.png": b"x" * (MAX_MEDIA_BYTES + 1)})
    assert material("sample.docx", zipped).reason == "docx_media_expansion_limit"


def test_all_completed_collections_historical_source_and_snapshot_version(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "counter.py").write_text("value = -1\n")
    (root / "pointer").write_text("counter.py")
    git("add", ".")
    git("commit", "-m", "initial counter")
    first_oid = git("rev-parse", "HEAD").decode()
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="synthetic", status="ongoing", base_branch="main")
    )
    pid, first = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, first, lambda: False)
    (root / "counter.py").write_text("value = 0\n")
    (root / "pointer").unlink()
    (root / "pointer").symlink_to("counter.py")
    git("add", ".")
    git("commit", "-m", "correct counter")
    second = str(projects.collect_now(pid)["id"])
    collect(db, second, lambda: False)
    materials = Materials(db)
    pdf_material = materials.add(pid, "result.pdf", pdf())
    base = AnalysisInput(db, materials).prepare(pid, enforce_total_budget=False)
    before = git("status", "--porcelain=v1"), git("rev-parse", "HEAD"), git("ls-files", "--stage")
    frozen = ExperienceInputs(db, materials).expand(base)
    assert [str(c["id"]) for c in json.loads(frozen.collections_json)] == [first, second]
    source = [e for e in frozen.entries if "/source/" in e.reference]
    assert any(
        json.loads(e.source_json)["commit_oid"] == first_oid and e.text == "value = -1\n"
        for e in source
    )
    assert any(e.text == "value = 0\n" for e in source)
    assert all("read_at" in json.loads(e.source_json) for e in source)
    heads = [e for e in frozen.entries if "/head_files/" in e.reference]
    assert len(heads) == 4
    assert len([e for e in heads if e.reason is None and e.text is not None]) == 3
    excluded_link = [e for e in heads if e.reason == "symlink"]
    assert len(excluded_link) == 1 and excluded_link[0].text is None
    assert json.loads(excluded_link[0].source_json)["collection_id"] == second
    assert any(
        e.reference == f"material/{pdf_material['id']}" and e.media and not e.reason
        for e in frozen.entries
    )
    assert before == (
        git("status", "--porcelain=v1"),
        git("rev-parse", "HEAD"),
        git("ls-files", "--stage"),
    )
    assert frozen.manifest()["transfer_enabled"] is False
    assert all("test@example.invalid" not in (e.text or "") for e in frozen.entries)
    digest = frozen.digest
    materials.delete(pid, str(pdf_material["id"]))
    failed = ExperienceInputs(db, materials).expand(base)
    missing = next(e for e in failed.entries if e.reference == f"material/{pdf_material['id']}")
    assert missing.reason == "not_found" and not missing.media
    assert (
        frozen.digest == digest
        and next(e for e in frozen.entries if e.reference == f"material/{pdf_material['id']}").media
    )


def test_missing_git_and_aggregate_budget_do_not_silently_hide_input(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "counter.py").write_text("value = 0\n")
    git("add", ".")
    git("commit", "-m", "counter")
    p = Projects(db).register(
        Registration(path=str(root), name="synthetic", status="ongoing", base_branch="main")
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    materials = Materials(db)
    frozen = ExperienceInputs(db, materials).prepare(pid)
    (root / ".git").rename(root / ".git-held")
    missing = ExperienceInputs(db, materials).prepare(pid)
    assert any(e.reason and "historical_tree_unavailable" in e.limitations for e in missing.entries)
    assert any(e.text == "value = 0\n" for e in missing.entries)
    assert any(e.text == "value = 0\n" and "/source/" in e.reference for e in frozen.entries)
    # A synthetic prepared source can exceed 16 MiB total without whole-project clipping.
    text = "counter " * 131072
    large = replace(
        frozen,
        entries=tuple(replace(frozen.entries[0], reference=str(i), text=text) for i in range(18)),
    )
    chunks = split_evidence(large, Limits())
    assert {ref for c in chunks for ref in c.references} == {str(i) for i in range(18)}
