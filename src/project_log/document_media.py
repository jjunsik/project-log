"""Local, fail-closed document preprocessing; original data is never modified."""

import io
import json
import os
import platform
import posixpath
import re
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from project_log.analysis_input import canonical
from project_log.git import CollectionError
from project_log.policy import MAX_BODY, has_secret, sha256

MAX_ORIGINAL_BYTES = 20 * MAX_BODY
MAX_RENDERED_BYTES = 64 * MAX_BODY
# Application budget, below documented 100 MB inline / 50 MB PDF limits. It is
# NOT a model/account acceptance measurement. Check the final SDK request too.
MAX_INLINE_REQUEST_BYTES = 32 * MAX_BODY
MAX_RESIDENT_BYTES = 2 * 1024 * MAX_BODY
MAX_SCRATCH_BYTES = 128 * MAX_BODY
WORKER = Path(__file__).with_name("_media_worker.py").resolve()
RUNTIME_PIN = Path(__file__).with_name("libreoffice_runtime.json")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
REL_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass(frozen=True)
class PDFPart:
    raw: bytes
    start: int
    end: int
    pages: int


@dataclass(frozen=True)
class PDFPreparation:
    parts: tuple[PDFPart, ...]
    limitations: tuple[str, ...]


def sandbox_profile(
    work: Path,
    executables: tuple[Path, ...],
    reads: tuple[Path, ...],
    *,
    ipc: Path | None = None,
) -> str:
    def quoted(path: Path) -> str:
        return json.dumps(str(path.resolve()), ensure_ascii=True)

    code = [Path(sys.base_prefix), Path(sys.prefix), *reads]
    # The python.org macOS executable is a trampoline into this same pinned
    # framework's Python.app, not an arbitrary child-program permission.
    framework_python = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
    if framework_python.is_file():
        executables = (*executables, framework_python)
    rules = [
        '(version 1) (deny default) (import "dyld-support.sb")',
        "(allow sysctl-read)",
        "(allow process-info* (target self))",
        "(allow process-exec " + " ".join(f"(literal {quoted(p)})" for p in executables) + ")",
        '(allow file-read* file-map-executable (subpath "/System/Library") '
        '(subpath "/usr/lib") (subpath "/usr/share") (subpath "/Library/Fonts") '
        '(subpath "/Library/Apple") ' + " ".join(f"(subpath {quoted(p)})" for p in code) + ")",
        '(allow file-read* (literal "/private/etc/localtime") '
        '(subpath "/private/var/db/timezone") '
        '(literal "/dev/random") (literal "/dev/urandom"))',
        '(allow file-read* file-write-data (literal "/dev/null"))',
        f"(allow file-read* file-write* (subpath {quoted(work)}))",
        "(allow file-read* " + f"(literal {quoted(WORKER)}))",
        "(allow file-read-metadata file-read-xattr "
        + " ".join(f"(path-ancestors {quoted(p)})" for p in [work, WORKER, *code, *executables])
        + ")",
    ]
    if ipc is not None:
        # LibreOffice's single-instance pipe stays inside this private job only.
        # No IP sockets, other Unix sockets or Mach services are authorized.
        rules.append(
            "(allow network-bind network-inbound network-outbound " + f"(subpath {quoted(ipc)}))"
        )
    return "\n".join(rules)


def watch_worker(process: subprocess.Popen[bytes], work: Path) -> int:
    deadline = time.monotonic() + 45
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise CollectionError("media_processing_timeout", "문서 처리 시간 제한 초과입니다.")
            sample = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            if process.poll() is not None:
                break
            try:
                resident = int(sample.stdout.strip()) * 1024
            except ValueError:
                raise CollectionError(
                    "media_resource_isolation_unavailable", "메모리 감시를 적용할 수 없습니다."
                ) from None
            if sample.returncode or resident > MAX_RESIDENT_BYTES:
                raise CollectionError("media_memory_limit", "문서 처리 메모리 제한 초과입니다.")
            files = list(work.rglob("*"))
            scratch = 0
            for file in files:
                try:
                    scratch += file.lstat().st_size
                except FileNotFoundError:
                    # LibreOffice deletes/renames its own temporary files while
                    # a sample is collected. Other stat failures still stop it.
                    continue
            if len(files) > 4096 or scratch > MAX_SCRATCH_BYTES:
                raise CollectionError("media_scratch_limit", "문서 처리 임시 공간 제한 초과입니다.")
            time.sleep(0.1)
        return process.wait()
    except (CollectionError, OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


def sandbox_run(work: Path, mode: str, *, binary: Path | None = None) -> None:
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise CollectionError(
            "media_sandbox_unavailable", "안전한 문서 처리 격리를 적용할 수 없습니다."
        )
    python = Path(sys.executable).absolute()
    reads = (binary.parents[2],) if binary else ()
    ipc = work / "ipc" if binary else None
    profile = sandbox_profile(work, (python, binary) if binary else (python,), reads, ipc=ipc)
    env = {
        "PATH": str(python.parent),
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SAL_DISABLE_OPENCL": "1",
    }
    for name in ("home", "tmp"):
        (work / name).mkdir(exist_ok=True)
    if ipc is not None:
        ipc.mkdir(mode=0o700, exist_ok=True)
        # TMPDIR does not control sal/osl/unx/pipe.cxx's fallback socket path.
        env["OSL_SOCKET_PATH"] = str(ipc)
    try:
        with (work / "stdout.log").open("wb") as out, (work / "stderr.log").open("wb") as err:
            process = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", str(WORKER), mode],
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            status = watch_worker(process, work)
        (work / "exit.json").write_text(canonical({"exit_status": status}))
        if status != 0:
            result = work / "result.json"
            if result.is_file() and not result.is_symlink() and result.stat().st_size <= 1024:
                failure = json.loads(result.read_text())
                if failure.get("error") == "media_resource_isolation_unavailable":
                    raise CollectionError(
                        "media_resource_isolation_unavailable",
                        "문서 처리 자원 격리를 적용할 수 없습니다.",
                    )
            raise CollectionError(
                "media_processing_failed", "격리된 문서 처리 실행에 실패했습니다."
            )
    except OSError:
        raise CollectionError(
            "media_sandbox_unavailable", "문서 처리 격리 실행에 실패했습니다."
        ) from None
    except subprocess.TimeoutExpired:
        raise CollectionError(
            "media_resource_isolation_unavailable", "문서 처리 자원 감시 실패입니다."
        ) from None


def prepare_pdf(raw: bytes) -> PDFPreparation:
    if len(raw) > MAX_RENDERED_BYTES:
        raise CollectionError("pdf_derivative_size_limit", "PDF 처리 크기 제한 초과입니다.")
    with tempfile.TemporaryDirectory(prefix="project-log-pdf-") as directory:
        work = Path(directory).resolve()
        (work / "input.pdf").write_bytes(raw)
        sandbox_run(work, "pdf")
        result_file = work / "result.json"
        if not result_file.is_file() or result_file.stat().st_size > MAX_BODY:
            raise CollectionError("pdf_parse_failed", "PDF 구조를 확인하지 못했습니다.")
        result = json.loads(result_file.read_text())
        if result.get("error"):
            raise CollectionError(
                result["error"], "PDF를 분석 가능한 페이지로 준비하지 못했습니다."
            )
        parts: list[PDFPart] = []
        covered: set[int] = set()
        for part in result["parts"]:
            filename = part["file"]
            if PurePosixPath(filename).name != filename:
                raise CollectionError("pdf_parse_failed", "잘못된 PDF 분할 결과입니다.")
            file = work / filename
            if file.is_symlink() or file.stat().st_size > MAX_ORIGINAL_BYTES:
                raise CollectionError("pdf_derivative_size_limit", "PDF 분할 크기 제한 초과입니다.")
            start, end, pages = part["start"], part["end"], result["pages"]
            if not 1 <= start <= end <= pages or end - start >= 32:
                raise CollectionError(
                    "pdf_page_coverage_invalid", "PDF 페이지 범위가 잘못됐습니다."
                )
            covered.update(range(start, end + 1))
            parts.append(PDFPart(file.read_bytes(), start, end, pages))
        if covered != set(range(1, result["pages"] + 1)):
            raise CollectionError("pdf_page_coverage_invalid", "PDF 페이지가 누락됐습니다.")
        return PDFPreparation(tuple(parts), tuple(result["limitations"]))


def render_input(raw: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Inspect the complete package before opening it; flatten external hyperlinks only."""
    notes: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if (
            len(infos) > 2048
            or len({i.filename for i in infos}) != len(infos)
            or sum(i.file_size for i in infos) > MAX_RENDERED_BYTES
        ):
            raise CollectionError(
                "docx_render_expansion_limit", "DOCX 변환의 압축 해제 제한 초과입니다."
            )
        xml_size = sum(
            i.file_size for i in infos if i.filename.lower().endswith((".xml", ".rels", ".svg"))
        )
        if xml_size > 4 * MAX_BODY:
            raise CollectionError("docx_render_xml_limit", "DOCX 변환 XML 제한 초과입니다.")
        replacements: dict[str, bytes] = {}
        # OOXML may name an XML part without an .xml suffix; inspect MIME-declared
        # XML too, so relationships/active content cannot evade the safety pass.
        types_raw = archive.read("[Content_Types].xml")
        if b"<!DOCTYPE" in types_raw or b"<!ENTITY" in types_raw or b"\0" in types_raw:
            raise CollectionError("docx_unsafe_package", "안전하지 않은 DOCX 형식 정의입니다.")
        content_types = ElementTree.fromstring(types_raw.decode("utf-8-sig"))
        overrides = {
            node.get("PartName", "").lstrip("/"): node.get("ContentType", "")
            for node in content_types
            if node.get("PartName")
        }
        defaults = {
            node.get("Extension", "").lower(): node.get("ContentType", "")
            for node in content_types
            if node.get("Extension")
        }
        xml_parts = {
            i.filename
            for i in infos
            if "xml"
            in overrides.get(
                i.filename, defaults.get(PurePosixPath(i.filename).suffix.lstrip(".").lower(), "")
            ).lower()
        }
        if (
            sum(
                i.file_size
                for i in infos
                if i.filename in xml_parts or i.filename.lower().endswith((".xml", ".rels", ".svg"))
            )
            > 4 * MAX_BODY
        ):
            raise CollectionError("docx_render_xml_limit", "DOCX 변환 XML 제한 초과입니다.")
        external_hyperlinks: set[str] = set()
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in PurePosixPath(name).parts or "\\" in name:
                raise CollectionError(
                    "docx_unsafe_package", "안전하지 않은 DOCX package 경로입니다."
                )
            lower = name.lower()
            if info.flag_bits & 1 or any(x in lower for x in ("vbaproject", "activex", "customui")):
                raise CollectionError(
                    "docx_active_content_requires_review", "활성 DOCX 내용은 변환하지 않습니다."
                )
            if name.startswith("word/embeddings/"):
                raise CollectionError(
                    "docx_embedded_objects_render_deferred",
                    "첨부 객체가 있는 DOCX의 시각 변환은 보류합니다.",
                )
            if not lower.endswith((".xml", ".rels", ".svg")) and name not in xml_parts:
                continue
            data = archive.read(info)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data or b"\0" in data or has_secret(data):
                raise CollectionError("docx_unsafe_package", "안전하지 않은 DOCX 변환 내용입니다.")
            tree = ElementTree.fromstring(data.decode("utf-8-sig"))
            fields = "".join(
                (node.text or "")
                if node.tag.rsplit("}", 1)[-1] == "instrText"
                else " " + node.get(WORD + "instr", "") + " "
                for node in tree.iter()
                if node.tag.rsplit("}", 1)[-1] in {"instrText", "fldSimple"}
            )
            if re.search(
                r"\b(?:DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK|DATABASE|IMPORT)\b",
                fields,
                re.I,
            ):
                raise CollectionError(
                    "docx_active_content_requires_review",
                    "외부·활성 Word 필드는 변환하지 않습니다.",
                )
            if name == "word/document.xml" and tree.tag != WORD + "document":
                raise CollectionError(
                    "docx_word_format_render_deferred",
                    "시각 변환을 검증하지 않은 Word XML 형식입니다.",
                )
            if name == "[Content_Types].xml" and any(
                any(x in v.lower() for x in ("macroenabled", "vbaproject", "activex"))
                for node in tree.iter()
                for v in node.attrib.values()
            ):
                raise CollectionError(
                    "docx_active_content_requires_review", "활성 DOCX 형식은 보류합니다."
                )
            for node in tree.iter():
                local = node.tag.rsplit("}", 1)[-1].lower()
                if local in {"oleobject", "object", "control"}:
                    raise CollectionError(
                        "docx_embedded_objects_render_deferred",
                        "특수·활성 객체 시각 변환은 보류합니다.",
                    )
                if node.tag in {WORD + "ins", WORD + "del", WORD + "moveFrom", WORD + "moveTo"} or (
                    node.tag.startswith(WORD) and local.endswith("change")
                ):
                    raise CollectionError(
                        "docx_tracked_changes_requires_review", "변경 추적 문서는 보류합니다."
                    )
                if node.tag == WORD + "altChunk":
                    raise CollectionError(
                        "docx_altchunk_render_deferred", "altChunk 시각 변환은 보류합니다."
                    )
                if node.tag.rsplit("}", 1)[-1] == "Relationship":
                    kind, target = node.get("Type", ""), node.get("Target", "")
                    if node.get("TargetMode", "").lower() == "external":
                        if kind.endswith("/hyperlink"):
                            external_hyperlinks.add(node.get("Id", ""))
                            tree.remove(node)
                            notes.add("docx_external_hyperlinks_not_loaded")
                            replacements[name] = ElementTree.tostring(
                                tree, encoding="utf-8", xml_declaration=True
                            )
                        else:
                            raise CollectionError(
                                "docx_external_content_render_deferred",
                                "외부 연결 내용의 시각 변환은 보류합니다.",
                            )
                    elif not kind.endswith("/hyperlink"):
                        owner = str(PurePosixPath(name).parent.parent)
                        resolved = posixpath.normpath(
                            target.lstrip("/")
                            if target.startswith("/")
                            else posixpath.join(owner, target)
                        )
                        if (
                            "\\" in target
                            or ":" in target
                            or resolved.startswith("../")
                            or resolved not in archive.namelist()
                        ):
                            raise CollectionError(
                                "docx_unsafe_package", "안전하지 않은 DOCX 연결입니다."
                            )
                mime = overrides.get(
                    name, defaults.get(PurePosixPath(name).suffix.lstrip(".").lower(), "")
                )
                if (
                    lower.endswith(".svg")
                    or "svg" in mime.lower()
                    or tree.tag.rsplit("}", 1)[-1] == "svg"
                ):
                    if (
                        node.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject", "style"}
                        or any(k.rsplit("}", 1)[-1].lower().startswith("on") for k in node.attrib)
                        or any(
                            ("href" in k and not v.startswith("#")) or "url(" in v.lower()
                            for k, v in node.attrib.items()
                        )
                    ):
                        raise CollectionError(
                            "docx_active_content_requires_review",
                            "외부·활성 SVG 내용은 변환하지 않습니다.",
                        )
                if local == "imagedata" and any(
                    k.rsplit("}", 1)[-1] in {"src", "href"} and v and not v.startswith("#")
                    for k, v in node.attrib.items()
                ):
                    raise CollectionError(
                        "docx_external_content_render_deferred",
                        "직접 연결된 VML 이미지의 변환은 보류합니다.",
                    )
        if not external_hyperlinks:
            return raw, tuple(sorted(notes))
        # Remove all external hyperlink IDs in Word XML while preserving visible text.
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as clean:
            for info in infos:
                body = replacements.get(info.filename, archive.read(info))
                if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                    tree = ElementTree.fromstring(body.decode("utf-8-sig"))
                    for node in tree.iter(WORD + "hyperlink"):
                        if node.get(REL_ATTR) in external_hyperlinks:
                            node.attrib.pop(REL_ATTR, None)
                    body = ElementTree.tostring(tree, encoding="utf-8", xml_declaration=True)
                clean.writestr(info.filename, body)
        return output.getvalue(), tuple(sorted(notes))


def conversion_services(binary: Path) -> bytes:
    """Job-local UNO registry without the GUI-initializing native spell checker.

    The signed Runtime remains unchanged. No optional script-provider registries
    or user/remote extension registries are added to this conversion process.
    """
    namespace = "{http://openoffice.org/2010/uno-components}"
    source = binary.parents[1] / "Resources/services/services.rdb"
    try:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * MAX_BODY:
            raise ValueError("unexpected runtime registry")
        tree = ElementTree.fromstring(source.read_bytes())
        if tree.tag != namespace + "components":
            raise ValueError("unexpected runtime registry")
        removed = 0
        for component in list(tree):
            for implementation in list(component):
                if implementation.get("name") == "org.openoffice.lingu.MacOSXSpellChecker":
                    if implementation.get("constructor") != (
                        "lingucomponent_MacSpellChecker_get_implementation"
                    ):
                        raise ValueError("unexpected spell checker")
                    component.remove(implementation)
                    removed += 1
            # UNO requires at least one implementation in every component.
            if not len(component):
                tree.remove(component)
        if removed != 1:
            raise ValueError("unexpected spell checker count")
        # Preserve the registry's default namespace without changing ElementTree's
        # global namespace map (other jobs also serialize Word XML).
        for node in tree.iter():
            if not node.tag.startswith(namespace):
                raise ValueError("unexpected registry namespace")
            node.tag = node.tag.removeprefix(namespace)
        tree.set("xmlns", namespace[1:-1])
        registry: bytes = ElementTree.tostring(tree, encoding="utf-8", xml_declaration=True)
        return registry
    except (OSError, ValueError, ElementTree.ParseError):
        raise CollectionError(
            "docx_renderer_unavailable", "승인된 변환 서비스 구성을 준비할 수 없습니다."
        ) from None


def render_docx(raw: bytes) -> tuple[bytes, dict[str, str], tuple[str, ...]]:
    sanitized, notes = render_input(raw)
    pin = json.loads(RUNTIME_PIN.read_text())
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise CollectionError(
            "docx_renderer_platform_unsupported", "검증된 DOCX 변환 Runtime이 없는 환경입니다."
        )
    runtime = (
        Path(__file__).resolve().parents[2] / ".local/runtimes" / ("libreoffice-" + pin["version"])
    )
    installed = runtime / "install.json"
    binary = runtime / "LibreOffice.app/Contents/MacOS/soffice"
    if not installed.is_file() or json.loads(installed.read_text()) != pin or not binary.is_file():
        raise CollectionError(
            "docx_renderer_unavailable", "승인된 로컬 DOCX 변환 Runtime이 없습니다."
        )
    # macOS Unix socket paths are bounded; the default per-user TMPDIR is too
    # long once LibreOffice adds its IPC identifier. This directory is mode 0700.
    with tempfile.TemporaryDirectory(prefix="pl-dx-", dir="/private/tmp") as directory:
        work = Path(directory).resolve()
        (work / "document.docx").write_bytes(sanitized)
        services = conversion_services(binary)
        (work / "services.rdb").write_bytes(services)
        profile = work / "profile/user"
        profile.mkdir(parents=True)
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            "</oor:items>"
        )
        options = canonical(
            {
                "UseLosslessCompression": {"type": "boolean", "value": "true"},
                "ReduceImageResolution": {"type": "boolean", "value": "false"},
                "ExportFormFields": {"type": "boolean", "value": "false"},
                "IsAddStream": {"type": "boolean", "value": "false"},
            }
        )
        command = [
            str(binary),
            "-env:UserInstallation=" + (work / "profile").as_uri(),
            "-env:URE_MORE_SERVICES=" + (work / "services.rdb").as_uri(),
            "--headless",
            "--norestore",
            "--nodefault",
            "--convert-to",
            "pdf:writer_pdf_Export:" + options,
            "--outdir",
            str(work),
            str(work / "document.docx"),
        ]
        (work / "command.json").write_text(canonical(command))
        sandbox_run(work, "render", binary=binary)
        output = work / "document.pdf"
        if (
            not output.is_file()
            or output.is_symlink()
            or output.stat().st_size > MAX_RENDERED_BYTES
        ):
            raise CollectionError(
                "docx_conversion_failed", "DOCX의 PDF 변환 결과를 확보하지 못했습니다."
            )
        converted = output.read_bytes()
        if not converted.startswith(b"%PDF-"):
            raise CollectionError("docx_conversion_failed", "DOCX 변환 결과가 PDF가 아닙니다.")
        return (
            converted,
            {
                "transform": "docx-to-pdf",
                "renderer": "LibreOffice",
                "renderer_version": pin["version"],
                "runtime_sha256": pin["sha256"],
                "service_policy": "docx-headless-v1",
                "services_sha256": sha256(services),
                "render_input_sha256": sha256(sanitized),
                "pdf_sha256": sha256(converted),
                "options": options,
            },
            notes,
        )
