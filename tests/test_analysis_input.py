import io
import json
import zipfile
from dataclasses import FrozenInstanceError

import pytest

from project_log.analysis_input import AnalysisInput, docx_text, material_text
from project_log.collector import collect
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.policy import MAX_BODY, sha256
from project_log.service import Projects, Registration


def docx(xml, extras=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
        for name, body in (extras or {}).items():
            archive.writestr(name, body)
    return stream.getvalue()


def word(text):
    return (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )


@pytest.mark.parametrize("extension", ["txt", "md", "log", "json", "csv"])
def test_text_formats_bom_and_safety(extension):
    body, reason, limitations = material_text(f"file.{extension}", b"\xef\xbb\xbfexample")
    assert (body, reason, limitations) == (b"example", None, ())
    assert (
        material_text(f"file.{extension}", b"api_key=syntheticsecretvalue")[1] == "suspected_secret"
    )
    assert material_text(f"file.{extension}", b"a\0b")[1] == "invalid_format"
    assert material_text(f"file.{extension}", b"x" * (MAX_BODY + 1))[1] == "large"
    assert material_text("credentials.txt", b"example")[1] == "sensitive_path"


@pytest.mark.parametrize("extension", ["pdf", "png", "jpg", "jpeg"])
def test_pending_formats_are_not_claimed_as_processed(extension):
    assert material_text(f"file.{extension}", b"unparsed")[0:2] == (
        None,
        "format_processing_pending",
    )


def test_docx_text_and_unprocessed_components():
    raw = docx(word("확인된 변경"), {"word/media/image1.png": b"not transmitted"})
    body, reason, limitations = material_text("evidence.docx", raw)
    assert body == "확인된 변경".encode() and reason is None
    assert "docx_images_or_attachments_unprocessed" in limitations
    assert "docx_text_only_layout_not_preserved" in limitations
    assert material_text("evidence.docx", docx(word("api_key=syntheticsecretvalue")))[1] == (
        "suspected_secret"
    )
    assert material_text("evidence.docx", docx("<document/>"))[1] == "no_extractable_text"
    revised = word("text").replace("<w:r>", "<w:ins><w:r>").replace("</w:r>", "</w:r></w:ins>")
    assert material_text("evidence.docx", docx(revised))[1] == (
        "docx_tracked_changes_requires_review"
    )


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
        word("text").encode("utf-16"),
        "<broken",
        "x" * (4 * MAX_BODY + 1),
    ],
)
def test_docx_rejects_entities_encoding_invalid_and_expansion(xml):
    with pytest.raises(CollectionError) as exc:
        docx_text(docx(xml))
    assert exc.value.code == "invalid_docx"


def test_completed_collections_project_isolation_freeze_and_provenance(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "counter.py").write_text("return_value = 0\n")
    git("add", ".")
    git("commit", "-m", "Fix example counter")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("Observed an empty input case.\n")
    (root / ".env").write_text("PASSWORD=syntheticvalue\n")
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="example", status="ongoing", base_branch="main")
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    store = Materials(db)
    original = store.add(pid, "notes.md", b"This is a user supplied note.")
    secret = store.add(pid, "secret-note.txt", b"api_key=syntheticsecretvalue")
    pdf = store.add(pid, "report.pdf", b"%PDF-1.7\nnot parsed")
    snapshot = AnalysisInput(db, store).prepare(pid)
    manifest = snapshot.manifest()
    assert manifest["transfer_enabled"] is False and manifest["analyzed"] is False
    assert [str(c["id"]) for c in manifest["collections"]] == [cid]
    assert all(e["permission"] == "pending_user_confirmation" for e in manifest["entries"])
    assert any(e.text == "Observed an empty input case.\n" for e in snapshot.entries)
    commits = [e for e in snapshot.entries if "/commits/" in e.reference]
    assert len(commits) == 1 and "Fix example counter" in commits[0].text
    assert "test@example.invalid" not in commits[0].text
    assert "Synthetic Developer" not in commits[0].text
    assert any(e.reason == "sensitive_path" for e in snapshot.entries)
    assert any(e.reason == "historical_source_not_preserved" for e in snapshot.entries)
    user = {e.reference: e for e in snapshot.entries if e.reference.startswith("material/")}
    assert user[f"material/{original['id']}"].text == "This is a user supplied note."
    assert user[f"material/{secret['id']}"].reason == "suspected_secret"
    assert user[f"material/{pdf['id']}"].reason == "format_processing_pending"
    assert all(json.loads(e.source_json)["collection_id"] is None for e in user.values())
    before = snapshot.digest
    store.add(pid, "later.txt", b"later evidence")
    active = projects.collect_now(pid)
    current = AnalysisInput(db, store).prepare(pid)
    assert str(active["id"]) not in current.collections_json
    assert snapshot.digest == before and len(current.entries) == len(snapshot.entries) + 1
    collect(db, str(active["id"]), lambda: False)
    assert len(json.loads(AnalysisInput(db, store).prepare(pid).collections_json)) == 2
    assert snapshot.digest == before
    with pytest.raises(FrozenInstanceError):
        snapshot.policy = "changed"
    other = tmp_path / "other"
    other.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-b", "main", str(other)], check=True, capture_output=True)
    second = projects.register(
        Registration(path=str(other), name="second", status="ongoing", base_branch="main")
    )
    collect(db, str(second["collections"][0]["id"]), lambda: False)
    isolated = AnalysisInput(db, store).prepare(str(second["id"]))
    assert all(not e.reference.startswith(cid) for e in isolated.entries)
    assert not any(e.reference.startswith("material/") for e in isolated.entries)


def test_missing_or_corrupt_material_is_excluded(db, git, tmp_path):
    p = Projects(db).register(
        Registration(
            path=str(tmp_path / "repo"), name="example", status="ongoing", base_branch="main"
        )
    )
    collect(db, str(p["collections"][0]["id"]), lambda: False)
    store = Materials(db)
    material = store.add(str(p["id"]), "note.txt", b"original")
    target = store.path(str(p["id"]), str(material["id"]))
    target.write_bytes(b"modified")
    snapshot = AnalysisInput(db, store).prepare(str(p["id"]))
    entry = next(e for e in snapshot.entries if e.reference == f"material/{material['id']}")
    assert entry.reason == "storage_corrupt" and entry.text is None
    assert json.loads(entry.source_json)["raw_sha256"] == sha256(b"original")
    target.unlink()
    snapshot = AnalysisInput(db, store).prepare(str(p["id"]))
    assert snapshot.entries[-1].reason == "storage_unavailable"


def test_budget_exclusion_is_explicit_not_silent_truncation(db, git, tmp_path, monkeypatch):
    p = Projects(db).register(
        Registration(
            path=str(tmp_path / "repo"), name="example", status="ongoing", base_branch="main"
        )
    )
    store = Materials(db)
    store.add(str(p["id"]), "a.txt", b"1234")
    store.add(str(p["id"]), "b.txt", b"5678")
    monkeypatch.setattr("project_log.analysis_input.MAX_INPUT_BYTES", 5)
    snapshot = AnalysisInput(db, store).prepare(str(p["id"]))
    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].text == "1234"
    assert snapshot.entries[1].reason == "input_budget_exceeded"
    assert snapshot.entries[1].text is None
