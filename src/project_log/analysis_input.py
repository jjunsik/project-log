"""Local-only, immutable input preparation. No provider call or analysis lifecycle."""

import base64
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from project_log.db import RECORDS, Database, Row
from project_log.git import CollectionError
from project_log.materials import TEXT_FORMATS, Materials, validate_format
from project_log.policy import MAX_BODY, content_reason, sensitive_path, sha256

INPUT_POLICY = "analysis-input-v1"
MAX_INPUT_BYTES = 16 * MAX_BODY
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class InputEntry:
    reference: str
    source_json: str  # Local provenance, never implicitly part of a provider request.
    text: str | None
    sha256: str | None
    reason: str | None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputSnapshot:
    project_id: str
    prepared_at: str
    collections_json: str
    entries: tuple[InputEntry, ...]
    policy: str = INPUT_POLICY

    @property
    def digest(self) -> str:
        return sha256(canonical(self.manifest()).encode())

    def manifest(self) -> Row:
        return {
            "project_id": self.project_id,
            "prepared_at": self.prepared_at,
            "policy": self.policy,
            "collections": json.loads(self.collections_json),
            "transfer_enabled": False,
            "analyzed": False,
            "entries": [
                {
                    "reference": e.reference,
                    "source": json.loads(e.source_json),
                    "sha256": e.sha256,
                    "status": "excluded" if e.reason else "prepared_locally",
                    "reason": e.reason,
                    "permission": "pending_user_confirmation",
                    "limitations": list(e.limitations),
                }
                for e in self.entries
            ],
        }


def docx_text(raw: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Bounded text extraction; no ZIP extraction, external relationship or XML entity access."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 2048 or len({i.filename for i in infos}) != len(infos):
                raise ValueError("ZIP directory limit or duplicate entries")
            selected = [
                i
                for i in infos
                if i.filename == "word/document.xml"
                or i.filename in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
                or (
                    i.filename.startswith(("word/header", "word/footer"))
                    and i.filename.endswith(".xml")
                    and "/" not in i.filename.removeprefix("word/")
                )
            ]
            if sum(i.file_size for i in selected) > 4 * MAX_BODY:
                raise ValueError("Uncompressed XML limit")
            paragraphs: list[str] = []
            limitations = {"docx_text_only_layout_not_preserved"}
            if any(i.filename.startswith(("word/media/", "word/embeddings/")) for i in infos):
                limitations.add("docx_images_or_attachments_unprocessed")
            if any(i.filename == "word/numbering.xml" for i in infos):
                limitations.add("docx_generated_numbering_unprocessed")
            for info in sorted(selected, key=lambda i: i.filename):
                if info.flag_bits & 1:
                    raise ValueError("Encrypted ZIP entry")
                with archive.open(info) as stream:
                    xml = stream.read(4 * MAX_BODY + 1)
                if len(xml) > 4 * MAX_BODY or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                    raise ValueError("Unsafe XML")
                # UTF-16 declarations could evade the byte check above. Only UTF-8 XML is accepted.
                xml.decode("utf-8-sig")
                if b"\0" in xml:
                    raise ValueError("Unsupported XML encoding")
                root = ElementTree.fromstring(xml)
                for node in root.iter():
                    if node.tag == WORD_NS + "altChunk":
                        limitations.add("docx_altchunk_unprocessed")
                    if node.tag in {WORD_NS + "ins", WORD_NS + "del"}:
                        limitations.add("docx_tracked_changes_present")
                for paragraph in root.iter(WORD_NS + "p"):
                    text = "".join(
                        (node.text or "")
                        if node.tag in {WORD_NS + "t", WORD_NS + "delText"}
                        else "\t"
                        if node.tag == WORD_NS + "tab"
                        else "\n"
                        for node in paragraph.iter()
                        if node.tag
                        in {
                            WORD_NS + "t",
                            WORD_NS + "delText",
                            WORD_NS + "tab",
                            WORD_NS + "br",
                            WORD_NS + "cr",
                        }
                    )
                    if text:
                        paragraphs.append(text)
            result = "\n".join(paragraphs).encode()
            return result, tuple(sorted(limitations))
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise CollectionError("invalid_docx", "DOCX의 안전한 텍스트 추출에 실패했습니다.") from exc


def material_text(filename: str, raw: bytes) -> tuple[bytes | None, str | None, tuple[str, ...]]:
    extension = Path(filename).suffix.lower()
    if sensitive_path(filename):
        return None, "sensitive_path", ()
    if extension not in TEXT_FORMATS | {".docx"}:
        return None, "format_processing_pending", ()
    try:
        validate_format(filename, raw)
        if extension == ".docx":
            body, limitations = docx_text(raw)
        else:
            body, limitations = raw.decode("utf-8-sig").encode(), ()
        reason = (
            content_reason(body)
            or ("no_extractable_text" if not body else None)
            or (
                "docx_tracked_changes_requires_review"
                if "docx_tracked_changes_present" in limitations
                else None
            )
        )
        return None if reason else body, reason, limitations
    except CollectionError as exc:
        return None, exc.code, ()


class AnalysisInput:
    def __init__(self, db: Database, materials: Materials):
        self.db = db
        self.materials = materials

    def prepare(self, project: str) -> InputSnapshot:
        """Freeze completed observations in one read-only DB snapshot; no current source reads.

        This is preparation, not an accepted analysis request. Long-lived project mutation
        locks, durable run storage and consent UI must precede enabling real transfers.
        """
        entries: list[InputEntry] = []
        used = 0

        def add(
            reference: str,
            source: Row,
            body: bytes | None,
            reason: str | None,
            paths: tuple[str, ...] = (),
            limitations: tuple[str, ...] = (),
        ) -> None:
            nonlocal used
            if any(sensitive_path(path) for path in paths):
                reason = "sensitive_path"
            if not reason and body is not None:
                reason = content_reason(body)
            if not reason and body is None:
                reason = "body_not_preserved"
            if not reason and body is not None and used + len(body) > MAX_INPUT_BYTES:
                reason = "input_budget_exceeded"
            if reason:
                body = None
            elif body is not None:
                used += len(body)
            entries.append(
                InputEntry(
                    reference,
                    canonical(source),
                    body.decode("utf-8") if body is not None else None,
                    sha256(body) if body is not None else None,
                    reason,
                    limitations,
                )
            )

        def path(meta: Row) -> tuple[str, str | None]:
            try:
                return base64.b64decode(meta["path_b64"], validate=True).decode("utf-8"), None
            except (KeyError, ValueError, UnicodeError):
                return "", "path_encoding_unavailable"

        with self.db.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if not conn.execute("SELECT id FROM projects WHERE id=%s", (project,)).fetchone():
                raise CollectionError("not_found", "해당 Project가 없습니다.")
            collections = conn.execute(
                "SELECT id,kind,retry_of,baseline_id,snapshot,policy,"
                "created_at,started_at,finished_at "
                "FROM collections "
                "WHERE project_id=%s AND state='completed' ORDER BY created_at,id",
                (project,),
            ).fetchall()
            for collection in collections:
                cid = str(collection["id"])
                for kind in ("commits", "changes", "working_entries", "head_files"):
                    order = "oid" if kind == "commits" else "id"
                    rows = conn.execute(
                        f"SELECT * FROM {RECORDS[kind]} WHERE project_id=%s "
                        f"AND collection_id=%s ORDER BY {order}",
                        (project, cid),
                    )
                    for row in rows:
                        meta = row["metadata"]
                        source = {
                            "collection_id": cid,
                            "kind": kind,
                            "record_id": str(row.get("id", row.get("oid"))),
                            "observed_at": row["observed_at"],
                        }
                        ref = f"{cid}/{kind}/{source['record_id']}"
                        if kind == "commits":
                            # Author/committer names, email and raw headers are personal data.
                            source["oid"] = row["oid"]
                            commit = {
                                "oid": row["oid"],
                                "parents": meta.get("parents", []),
                                "message": meta.get("message"),
                                "author_time": {
                                    key: meta.get("author", {}).get(key)
                                    for key in ("timestamp", "timezone")
                                },
                            }
                            add(
                                ref,
                                source,
                                canonical(commit).encode(),
                                meta.get("message_reason"),
                                limitations=("commit_personal_headers_excluded",),
                            )
                        elif kind == "changes":
                            old, old_reason = path(meta["old"])
                            new, new_reason = path(meta["new"])
                            source.update(
                                commit_oid=row["commit_oid"],
                                parent_oid=row["parent_oid"],
                                metadata=meta,
                                old_path_b64=meta["old"]["path_b64"],
                                new_path_b64=meta["new"]["path_b64"],
                            )
                            add(
                                ref,
                                source,
                                bytes(row["diff"]) if row["diff"] is not None else None,
                                old_reason or new_reason or row["body_reason"],
                                (old, new),
                            )
                        elif kind == "working_entries":
                            name, path_reason = path(meta)
                            source.update(
                                layer=row["layer"],
                                metadata=meta,
                                path_b64=meta["path_b64"],
                                raw_sha256=row["sha256"],
                                body_observed_at=row["body_observed_at"],
                                provenance=row["provenance"],
                                original_body_reason=row["original_body_reason"],
                                repaired=row["repaired"],
                                repair_read_metadata=row["repair_read_metadata"],
                            )
                            add(
                                ref,
                                source,
                                bytes(row["body"]) if row["body"] is not None else None,
                                path_reason or row["body_reason"],
                                (name,),
                            )
                        else:
                            name, path_reason = path(meta)
                            source.update(locator=meta, provenance="collection_git_locator_only")
                            add(
                                ref,
                                source,
                                None,
                                path_reason or "historical_source_not_preserved",
                                (name,),
                            )
            # Use this transaction's version, not a subsequent listing after an upload.
            materials = conn.execute(
                "SELECT * FROM user_materials WHERE project_id=%s ORDER BY added_at,id", (project,)
            ).fetchall()
            for material in materials:
                mid = str(material["id"])
                source = {
                    "kind": "user_material",
                    "material_id": mid,
                    "filename": material["filename"],
                    "raw_sha256": material["sha256"],
                    "size_bytes": material["size_bytes"],
                    "added_at": material["added_at"],
                    "provenance": "project_owned_copy",
                    "collection_id": None,
                }
                body, reason = None, None
                limitations: tuple[str, ...] = ()
                if material["extension"] not in TEXT_FORMATS | {".docx"}:
                    reason = "format_processing_pending"
                else:
                    try:
                        version, raw = self.materials.read(project, mid)
                        if version["sha256"] != material["sha256"]:
                            reason = "material_version_changed"
                        else:
                            body, reason, limitations = material_text(material["filename"], raw)
                    except CollectionError as exc:
                        reason = exc.code
                    except OSError:
                        reason = "storage_unavailable"
                add(f"material/{mid}", source, body, reason, limitations=limitations)
        return InputSnapshot(
            project,
            datetime.now(UTC).isoformat(),
            canonical(collections),
            tuple(entries),
        )
