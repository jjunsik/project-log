import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from media_fixtures import pdf, visual_docx
from pypdf import PdfReader, PdfWriter
from test_analysis_input import docx, word

from project_log.document_media import (
    conversion_services,
    prepare_pdf,
    render_docx,
    render_input,
    sandbox_profile,
)
from project_log.experience_input import material_evidence
from project_log.git import CollectionError
from project_log.policy import sha256


@pytest.fixture(autouse=True)
def synthetic_worker_diagnostics(monkeypatch):
    from project_log import document_media

    original = document_media.sandbox_run
    popen = subprocess.Popen
    watch = document_media.watch_worker

    def diagnostic_watch(*args):
        try:
            return watch(*args)
        except OSError as exc:
            print("synthetic watchdog diagnostic", repr(exc))
            raise

    monkeypatch.setattr(document_media, "watch_worker", diagnostic_watch)

    def diagnostic_popen(*args, **kwargs):
        if "HOME" in kwargs.get("env", {}):
            kwargs["env"] = {**kwargs["env"], "SAL_LOG": "+WARN+INFO"}
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", diagnostic_popen)

    def checked(work, mode, **kwargs):
        try:
            return original(work, mode, **kwargs)
        except CollectionError:
            # This module only creates synthetic inputs; production never exposes stderr.
            for name in ("stderr.log", "stdout.log", "result.json", "exit.json"):
                file = work / name
                if file.is_file():
                    print(name, file.read_bytes()[:8192].decode(errors="replace"))
            raise

    monkeypatch.setattr(document_media, "sandbox_run", checked)


def test_actual_sandbox_denies_network_other_files_writes_and_exec(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    canary = tmp_path / "canary"
    canary.write_text("synthetic-private-canary")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        snippet = f"""
import errno, json, os, socket, subprocess
def denied(fn):
    try: fn()
    except OSError as e: return e.errno in (errno.EPERM, errno.EACCES)
    return False
checks = {{
 "read": denied(lambda: open({str(canary)!r}).read()),
 "write": denied(lambda: open({str(canary)!r}, "w").write("changed")),
 "network": denied(lambda: socket.socket().connect(("127.0.0.1", {port}))),
 "exec": denied(lambda: subprocess.run(["/bin/echo", "escape"], check=True)),
 "env": "GEMINI_API_KEY" not in os.environ,
}}
print(json.dumps(checks))
"""
        python = Path(sys.executable)
        profile = sandbox_profile(work, (python,), ())
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", "-c", snippet],
            cwd=work,
            env={"PATH": str(python.parent), "HOME": str(work)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        k: True for k in ("read", "write", "network", "exec", "env")
    }
    assert canary.read_text() == "synthetic-private-canary"


def test_renderer_ipc_is_private_and_other_access_stays_denied():
    from project_log import document_media

    pin = json.loads(document_media.RUNTIME_PIN.read_text())
    binary = (
        document_media.RUNTIME_PIN.parents[2]
        / ".local/runtimes"
        / ("libreoffice-" + pin["version"])
        / "LibreOffice.app/Contents/MacOS/soffice"
    )
    with tempfile.TemporaryDirectory(prefix="pl-sb-", dir="/private/tmp") as directory:
        root = Path(directory).resolve()
        work = root / "job"
        ipc = work / "ipc"
        ipc.mkdir(parents=True)
        canary = root / "private-canary"
        canary.write_text("synthetic-only")
        with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
            tcp.bind(("127.0.0.1", 0))
            tcp.listen(1)
            unix.bind(str(root / "other.sock"))
            unix.listen(1)
            snippet = f"""
import ctypes, errno, json, socket, subprocess
def denied(fn):
    try: fn()
    except OSError as e: return e.errno in (errno.EPERM, errno.EACCES)
    return False
def private_ipc():
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind({str(ipc / "own.sock")!r})
        server.listen(1)
        client.connect({str(ipc / "own.sock")!r})
        connection, _ = server.accept()
        with connection:
            client.sendall(b'synthetic')
            return connection.recv(9) == b'synthetic'
system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap = ctypes.c_uint.in_dll(system, 'bootstrap_port').value
port = ctypes.c_uint()
checks = {{
 'private_ipc': private_ipc(),
 'other_unix': denied(lambda: socket.socket(socket.AF_UNIX).connect({str(root / "other.sock")!r})),
 'other_bind': denied(lambda: socket.socket(socket.AF_UNIX).bind({str(work / "escape.sock")!r})),
 'tcp_connect': denied(lambda: socket.socket().connect(('127.0.0.1', {tcp.getsockname()[1]}))),
 'tcp_bind': denied(lambda: socket.socket().bind(('127.0.0.1', 0))),
 'read': denied(lambda: open({str(canary)!r}).read()),
 'write': denied(lambda: open({str(canary)!r}, 'w').write('changed')),
 'exec': denied(lambda: subprocess.run(['/bin/echo', 'escape'], check=True)),
 'windowserver': system.bootstrap_look_up(
     bootstrap, b'com.apple.windowserver.active', ctypes.byref(port)) != 0,
}}
print(json.dumps(checks))
"""
            python = Path(sys.executable)
            profile = sandbox_profile(work, (python, binary), (binary.parents[2],), ipc=ipc)
            result = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", "-c", snippet],
                cwd=work,
                env={"PATH": str(python.parent), "HOME": str(work)},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        assert result.returncode == 0, result.stderr.decode()
        assert json.loads(result.stdout) == {
            name: True
            for name in (
                "private_ipc",
                "other_unix",
                "other_bind",
                "tcp_connect",
                "tcp_bind",
                "read",
                "write",
                "exec",
                "windowserver",
            )
        }
        assert canary.read_text() == "synthetic-only"


def test_conversion_services_preserves_runtime_and_all_other_implementations():
    from project_log import document_media

    pin = json.loads(document_media.RUNTIME_PIN.read_text())
    binary = (
        document_media.RUNTIME_PIN.parents[2]
        / ".local/runtimes"
        / ("libreoffice-" + pin["version"])
        / "LibreOffice.app/Contents/MacOS/soffice"
    )
    source = binary.parents[1] / "Resources/services/services.rdb"
    original = source.read_bytes()
    tree = ElementTree.fromstring(conversion_services(binary))
    tag = "{http://openoffice.org/2010/uno-components}implementation"
    retained = {node.get("name"): ElementTree.tostring(node) for node in tree.iter(tag)}
    expected = {
        node.get("name"): ElementTree.tostring(node)
        for node in ElementTree.fromstring(original).iter(tag)
        if node.get("name") != "org.openoffice.lingu.MacOSXSpellChecker"
    }
    assert retained == expected and all(len(component) for component in tree)
    assert source.read_bytes() == original


@pytest.mark.parametrize("constructor", [None, "unexpected_constructor"])
def test_unexpected_conversion_registry_fails_closed(tmp_path, constructor):
    source = tmp_path / "Contents/Resources/services/services.rdb"
    source.parent.mkdir(parents=True)
    item = (
        '<implementation name="org.openoffice.lingu.MacOSXSpellChecker" '
        f'constructor="{constructor}"/>'
        if constructor
        else ""
    )
    source.write_text(
        '<components xmlns="http://openoffice.org/2010/uno-components">'
        f"<component>{item}</component></components>"
    )
    with pytest.raises(CollectionError) as caught:
        conversion_services(tmp_path / "Contents/MacOS/soffice")
    assert caught.value.code == "docx_renderer_unavailable"


def test_pinned_runtime_bootstrap_without_opening_a_document(tmp_path):
    from project_log import document_media

    pin = json.loads(document_media.RUNTIME_PIN.read_text())
    binary = (
        document_media.RUNTIME_PIN.parents[2]
        / ".local/runtimes"
        / ("libreoffice-" + pin["version"])
        / "LibreOffice.app/Contents/MacOS/soffice"
    )
    (tmp_path / "command.json").write_text(json.dumps([str(binary), "--headless", "--version"]))
    document_media.sandbox_run(tmp_path, "render", binary=binary)
    assert pin["version"] in (tmp_path / "stdout.log").read_text()


@pytest.mark.parametrize(
    "parts,expected",
    [
        ({"word/vbaProject.bin": b"synthetic"}, "docx_active_content_requires_review"),
        ({"word/embeddings/object.bin": b"synthetic"}, "docx_embedded_objects_render_deferred"),
        (
            {"word/media/active.svg": b"<svg><script>synthetic()</script></svg>"},
            "docx_active_content_requires_review",
        ),
        (
            {"word/media/link.svg": b'<svg><image href="file:///synthetic-canary"/></svg>'},
            "docx_active_content_requires_review",
        ),
    ],
)
def test_active_or_embedded_content_is_rejected_before_runtime(parts, expected, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe input reached runtime")

    monkeypatch.setattr("project_log.document_media.sandbox_run", forbidden)
    with pytest.raises(CollectionError) as caught:
        render_docx(docx(word("counter"), parts))
    assert caught.value.code == expected


def test_external_content_and_tracked_changes_are_not_opened(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("External or tracked content reached runtime")

    monkeypatch.setattr("project_log.document_media.sandbox_run", forbidden)
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId2" Type="image" TargetMode="External" Target="file:///synthetic-canary"/>
    </Relationships>"""
    raw = docx(word("counter"), {"word/_rels/document.xml.rels": rels})
    with pytest.raises(CollectionError) as caught:
        render_docx(raw)
    assert caught.value.code == "docx_external_content_render_deferred"
    for element in ("moveFrom", "ins", "del", "rPrChange", "altChunk"):
        xml = (
            word("counter")
            .replace("<w:r>", f"<w:{element}><w:r>")
            .replace("</w:r>", f"</w:r></w:{element}>")
        )
        with pytest.raises(CollectionError) as caught:
            render_docx(docx(xml))
        assert caught.value.code == (
            "docx_altchunk_render_deferred"
            if element == "altChunk"
            else "docx_tracked_changes_requires_review"
        )
    active = (
        word("counter")
        .replace("<w:t>", "<w:instrText>DDEAUTO ")
        .replace("</w:t>", "</w:instrText>")
    )
    with pytest.raises(CollectionError) as caught:
        render_docx(docx(active))
    assert caught.value.code == "docx_active_content_requires_review"
    vml = word("counter").replace(
        "<w:r>",
        '<w:r><v:imagedata xmlns:v="urn:schemas-microsoft-com:vml" src="file:///synthetic-canary"/>',
    )
    with pytest.raises(CollectionError) as caught:
        render_docx(docx(vml))
    assert caught.value.code == "docx_external_content_render_deferred"


def test_external_hyperlinks_become_static_text_without_changing_original():
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1"
     Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
     TargetMode="External" Target="https://example.invalid"/>
    </Relationships>"""
    raw = docx(word("visible reference"), {"word/_rels/document.xml.rels": rels})
    digest = sha256(raw)
    sanitized, notes = render_input(raw)
    with zipfile.ZipFile(io.BytesIO(sanitized)) as archive:
        assert b"https://example.invalid" not in archive.read("word/_rels/document.xml.rels")
        assert b"visible reference" in archive.read("word/document.xml")
    assert sha256(raw) == digest
    assert notes == ("docx_external_hyperlinks_not_loaded",)


def test_real_docx_visual_pdf_and_original_links(tmp_path):
    raw = visual_docx()
    rendered, origin, notes = render_docx(raw)
    assert origin["service_policy"] == "docx-headless-v1"
    assert len(origin["services_sha256"]) == 64
    reader = PdfReader(io.BytesIO(rendered))
    assert len(reader.pages) == 2
    text = "\n".join(page.extract_text() for page in reader.pages)
    for marker in (
        "COUNTER-CORRECTION",
        "BEFORE -1",
        "AFTER 0",
        "SHAPE-MARKER",
        "VALIDATION-PAGE-2",
    ):
        assert marker in text
    assert any(
        obj.get_object().get("/Subtype") == "/Image"
        for page in reader.pages
        for obj in page["/Resources"].get("/XObject", {}).values()
    )
    assert origin["pdf_sha256"] == sha256(rendered) and notes == ()
    evidence = material_evidence("synthetic/docx", {"filename": "visual.docx"}, raw)
    assert evidence.reason is None and "AFTER 0" in evidence.text
    original = next(m for m in evidence.media if m.mime_type.endswith("wordprocessingml.document"))
    assert original.raw == raw and not original.use_for_analysis
    visual = next(m for m in evidence.media if m.mime_type == "application/pdf")
    metadata = json.loads(visual.provenance_json)
    assert metadata["original_sha256"] == sha256(raw)
    assert (metadata["page_start"], metadata["page_end"], metadata["document_pages"]) == (1, 2, 2)


def test_pdf_split_preserves_every_page_order_overlap_and_original_bytes():
    raw = pdf(70)
    digest = sha256(raw)
    result = prepare_pdf(raw)
    assert [(p.start, p.end) for p in result.parts] == [(1, 32), (32, 63), (63, 70)]
    assert {n for p in result.parts for n in range(p.start, p.end + 1)} == set(range(1, 71))
    for part in result.parts:
        reader = PdfReader(io.BytesIO(part.raw))
        assert len(reader.pages) == part.end - part.start + 1
        for offset, page in enumerate(reader.pages):
            assert f"SYNTHETIC-PAGE-{part.start + offset}" in page.extract_text()
    assert sha256(raw) == digest
    evidence = material_evidence("synthetic/pdf", {"filename": "pages.pdf"}, raw)
    assert evidence.media[0].raw == raw and not evidence.media[0].use_for_analysis
    assert len([m for m in evidence.media if m.use_for_analysis]) == 3


def test_invalid_encrypted_pdf_and_unavailable_sandbox_are_visible(monkeypatch):
    with pytest.raises(CollectionError) as caught:
        prepare_pdf(b"%PDF-1.7\ninvalid\n%%EOF")
    assert caught.value.code == "pdf_parse_failed"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("synthetic-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(CollectionError) as caught:
        prepare_pdf(encrypted.getvalue())
    assert caught.value.code == "pdf_encrypted_requires_review"
    monkeypatch.setattr("project_log.document_media.platform.system", lambda: "unsupported")
    evidence = material_evidence("synthetic/pdf", {"filename": "page.pdf"}, pdf())
    assert evidence.reason == "media_sandbox_unavailable"
    assert evidence.media[0].raw == pdf() and not evidence.media[0].use_for_analysis


def test_processing_failure_cleans_temporary_work_without_fallback(tmp_path, monkeypatch):
    from project_log import document_media

    seen = []

    def failed(work, mode, **kwargs):
        seen.append(work)
        assert (work / "document.docx").is_file()
        assert str(work).startswith("/private/tmp/pl-dx-")
        assert work.stat().st_mode & 0o777 == 0o700
        assert (work / "services.rdb").is_file()
        raise CollectionError("media_processing_failed", "synthetic failure")

    monkeypatch.setattr(document_media, "sandbox_run", failed)
    with pytest.raises(CollectionError) as caught:
        render_docx(visual_docx())
    assert caught.value.code == "media_processing_failed"
    assert seen and all(not p.exists() for p in seen)


@pytest.mark.parametrize(
    "sample,expected",
    [(b"3000000", "media_memory_limit"), (b"", "media_resource_isolation_unavailable")],
)
def test_watchdog_terminates_only_its_worker_when_memory_or_monitor_fails(
    tmp_path, monkeypatch, sample, expected
):
    from types import SimpleNamespace

    from project_log import document_media

    class Process:
        pid = 12345
        status = None

        def poll(self):
            return self.status

        def wait(self):
            assert self.status == -9
            return self.status

    process = Process()
    signals = []

    def kill(pid, signal):
        signals.append((pid, signal))
        process.status = -9

    monkeypatch.setattr(os, "killpg", kill)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=sample, returncode=0)
    )
    with pytest.raises(CollectionError) as caught:
        document_media.watch_worker(process, tmp_path)
    assert caught.value.code == expected
    assert signals == [(12345, 9)]


@pytest.mark.parametrize("removed", [True, False])
def test_watchdog_handles_removed_files_but_stops_on_access_failure(tmp_path, monkeypatch, removed):
    from types import SimpleNamespace

    from project_log import document_media

    temporary = tmp_path / "worker-temporary"
    temporary.write_bytes(b"synthetic")
    (tmp_path / "retained").write_bytes(b"synthetic")
    process = SimpleNamespace(pid=12345, status=None)
    process.poll = lambda: process.status
    process.wait = lambda: process.status
    signals = []

    def kill(pid, signal):
        signals.append((pid, signal))
        process.status = -9

    stat = Path.lstat

    def sampled(file, *args, **kwargs):
        if file == temporary:
            raise FileNotFoundError() if removed else PermissionError()
        return stat(file, *args, **kwargs)

    def finish(_):
        process.status = 0

    monkeypatch.setattr(Path, "lstat", sampled)
    monkeypatch.setattr(os, "killpg", kill)
    monkeypatch.setattr(document_media.time, "sleep", finish)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"1000", returncode=0)
    )
    if removed:
        assert document_media.watch_worker(process, tmp_path) == 0
        assert signals == []
    else:
        with pytest.raises(PermissionError):
            document_media.watch_worker(process, tmp_path)
        assert signals == [(12345, 9)]
