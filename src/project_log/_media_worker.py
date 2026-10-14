"""Bounded subprocess entry point; no project imports, credentials, plugins or networking."""

import io
import json
import os
import resource
import sys
from pathlib import Path

MAX_PART_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
PAGES_PER_PART = 32


def limits() -> None:
    for name, value in (
        ("RLIMIT_CORE", 0),
        ("RLIMIT_CPU", 30),
        # macOS rejects AS/DATA limits here; parent enforces a resident watchdog.
        ("RLIMIT_FSIZE", 128 * 1024 * 1024),
        ("RLIMIT_NOFILE", 128),
    ):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (OSError, ValueError) as exc:
            raise RuntimeError(name + ":" + type(exc).__name__) from None


def pdf_parts(root: Path) -> dict[str, object]:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject

    raw = (root / "input.pdf").read_bytes()
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted:
        return {"error": "pdf_encrypted_requires_review"}
    count = len(reader.pages)
    if not 0 < count <= 10000:
        return {"error": "pdf_page_count_limit"}
    notes: list[str] = []
    catalog = reader.trailer["/Root"]
    if not isinstance(catalog, DictionaryObject):
        return {"error": "pdf_parse_failed"}
    names = catalog.get("/Names")
    if names is not None:
        names = names.get_object()
    if isinstance(names, DictionaryObject) and names.get("/EmbeddedFiles"):
        notes.append("pdf_embedded_attachments_unprocessed")
    if any(page.get("/B") for page in reader.pages):
        notes.append("pdf_article_threads_not_preserved")
    if count <= PAGES_PER_PART and len(raw) <= MAX_PART_BYTES:
        return {
            "pages": count,
            "parts": [{"file": "input.pdf", "start": 1, "end": count}],
            "limitations": notes,
        }
    parts: list[dict[str, object]] = []
    start, total = 0, 0
    while start < count:
        end = min(count, start + PAGES_PER_PART)
        while True:
            writer = PdfWriter()
            for page in reader.pages[start:end]:
                # Article threads can clone pages outside the selected range. The original
                # remains preserved and this nonvisual structure is explicitly reported.
                writer.add_page(page, excluded_keys=["/B"])
            output = io.BytesIO()
            writer.write(output)
            body = output.getvalue()
            if len(body) <= MAX_PART_BYTES:
                break
            if end - start == 1:
                return {"error": "pdf_single_page_size_limit", "pages": count}
            end = start + max(1, (end - start) // 2)
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            return {"error": "pdf_derivative_size_limit", "pages": count}
        name = f"part-{len(parts):04}.pdf"
        (root / name).write_bytes(body)
        parts.append({"file": name, "start": start + 1, "end": end})
        # Overlap one page when possible; single-page parts always advance.
        start = end if end == count or end - start == 1 else end - 1
    return {"pages": count, "parts": parts, "limitations": notes}


def main() -> None:
    root = Path.cwd()
    try:
        limits()
    except RuntimeError as exc:
        (root / "result.json").write_text(
            json.dumps({"error": "media_resource_isolation_unavailable", "limit": str(exc)})
        )
        raise SystemExit(2) from None
    if sys.argv[1] == "render":
        args = json.loads((root / "command.json").read_text())
        os.execv(args[0], args)
    try:
        result = pdf_parts(root)
    except Exception:
        result = {"error": "pdf_parse_failed"}
    (root / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
