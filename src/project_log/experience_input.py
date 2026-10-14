"""Stage-2 evidence: frozen bytes and locators, without provider access or project writes."""

import io
import json
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from project_log.analysis_input import AnalysisInput, InputSnapshot, canonical, docx_text
from project_log.db import Database, Row
from project_log.document_media import prepare_pdf, render_docx
from project_log.git import CollectionError, diagnose
from project_log.materials import FORMATS, TEXT_FORMATS, Materials, validate_format
from project_log.policy import MAX_BODY, has_secret, sensitive_path, sha256

EXPERIENCE_INPUT_POLICY = "experience-input-v2"
MAX_MEDIA_BYTES = 20 * MAX_BODY


def text_reason(raw: bytes) -> str | None:
    # Stage 2 partitions large text; the collector's size policy remains unchanged.
    if b"\0" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
    except UnicodeError:
        return "non_utf8"
    return "suspected_secret" if has_secret(raw) else None


@dataclass(frozen=True)
class Media:
    name: str
    mime_type: str
    raw: bytes
    provenance_json: str = "{}"
    use_for_analysis: bool = True

    @property
    def digest(self) -> str:
        return sha256(self.raw)

    def descriptor(self) -> Row:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "sha256": self.digest,
            "size_bytes": len(self.raw),
            "provenance": json.loads(self.provenance_json),
            "use_for_analysis": self.use_for_analysis,
        }


@dataclass(frozen=True)
class Evidence:
    reference: str
    source_json: str
    text: str | None = None
    media: tuple[Media, ...] = ()
    reason: str | None = None
    limitations: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return sha256(
            canonical(
                {
                    "text": self.text,
                    "media": [
                        {
                            "sha256": m.digest,
                            "provenance": json.loads(m.provenance_json),
                            "use_for_analysis": m.use_for_analysis,
                        }
                        for m in self.media
                    ],
                }
            ).encode()
        )


@dataclass(frozen=True)
class ExperienceInput:
    project_id: str
    prepared_at: str
    collections_json: str
    entries: tuple[Evidence, ...]
    policy: str = EXPERIENCE_INPUT_POLICY

    def manifest(self) -> Row:
        return {
            "project_id": self.project_id,
            "prepared_at": self.prepared_at,
            "collections": json.loads(self.collections_json),
            "policy": self.policy,
            "transfer_enabled": False,
            "entries": [
                {
                    "reference": e.reference,
                    "source": json.loads(e.source_json),
                    "sha256": e.digest,
                    "reason": e.reason,
                    "limitations": list(e.limitations),
                    "media": [m.descriptor() for m in e.media],
                }
                for e in self.entries
            ],
        }

    @property
    def digest(self) -> str:
        return sha256(canonical(self.manifest()).encode())


def pdf_media(
    filename: str, raw: bytes, origin: Row | None = None
) -> tuple[tuple[Media, ...], tuple[str, ...]]:
    prepared = prepare_pdf(raw)
    origin = origin or {"transform": "original-pdf", "original_sha256": sha256(raw)}
    media: list[Media] = []
    if len(prepared.parts) > 1 or prepared.parts[0].raw != raw:
        media.append(Media(filename, "application/pdf", raw, canonical(origin), False))
    for part in prepared.parts:
        metadata = {
            **origin,
            "document_pages": part.pages,
            "page_start": part.start,
            "page_end": part.end,
            "page_numbering": "derived-pdf"
            if origin.get("transform") == "docx-to-pdf"
            else "original-pdf",
            "page_splitter": "pypdf-6.20.1",
            "page_overlap": 1 if len(prepared.parts) > 1 else 0,
        }
        name = (
            filename
            if len(prepared.parts) == 1
            else f"{filename}.pages-{part.start}-{part.end}.pdf"
        )
        media.append(Media(name, "application/pdf", part.raw, canonical(metadata)))
    return tuple(media), prepared.limitations


def material_evidence(reference: str, source: Row, raw: bytes) -> Evidence:
    """Prepare native PDF/images or text + DOCX media; never execute embedded content.

    Native media is untrusted and needs explicit human transmission review. A signature
    or byte-level secret scan does not verify its visual content or authorize transfer.
    """
    filename = source["filename"]
    extension = Path(filename).suffix.lower()
    base = Evidence(reference, canonical(source))
    if sensitive_path(filename):
        return replace(base, reason="sensitive_path")
    try:
        validate_format(filename, raw)
        if len(raw) > MAX_MEDIA_BYTES:
            return replace(base, reason="media_size_limit")
        if extension in {".pdf", ".png", ".jpg", ".jpeg"}:
            if has_secret(raw):
                return replace(base, reason="suspected_secret")
            media: tuple[Media, ...] = (Media(filename, FORMATS[extension], raw),)
            native_limits: tuple[str, ...] = (
                "native_media_requires_visual_privacy_review",
                "native_media_content_not_locally_verified",
            )
            if extension == ".pdf":
                try:
                    media, pdf_limits = pdf_media(filename, raw)
                    native_limits += pdf_limits
                except (CollectionError, OSError, ValueError) as exc:
                    return replace(
                        base,
                        media=(replace(media[0], use_for_analysis=False),),
                        reason=exc.code if isinstance(exc, CollectionError) else "pdf_parse_failed",
                        limitations=native_limits,
                    )
            return replace(
                base,
                media=media,
                limitations=native_limits,
            )
        if extension in TEXT_FORMATS:
            body = raw.decode("utf-8-sig").encode()
            reason = text_reason(body)
            return replace(base, text=body.decode() if not reason else None, reason=reason)
        body, limitations = docx_text(raw)
        reason = text_reason(body)
        if "docx_tracked_changes_present" in limitations:
            reason = "docx_tracked_changes_requires_review"
        if reason:
            return replace(base, reason=reason, limitations=limitations)
        images: list[Media] = []
        limits = set(limitations) - {"docx_images_or_attachments_unprocessed"}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            # docx_text already checked directory duplication/count/XML safety.
            media_infos = [i for i in archive.infolist() if i.filename.startswith("word/media/")]
            if sum(i.file_size for i in media_infos) > MAX_MEDIA_BYTES:
                return replace(base, reason="docx_media_expansion_limit", limitations=limitations)
            for info in media_infos:
                ext = Path(info.filename).suffix.lower()
                if ext not in {".png", ".jpg", ".jpeg"}:
                    limits.add("docx_unsupported_media:" + info.filename)
                    continue
                image = archive.read(info)
                validate_format(info.filename, image)
                if has_secret(image):
                    return replace(base, reason="suspected_secret")
                images.append(Media(info.filename, FORMATS[ext], image))
            if any(i.filename.startswith("word/embeddings/") for i in archive.infolist()):
                limits.add("docx_embedded_attachments_unprocessed")
        if images:
            limits.update(
                {
                    "native_media_requires_visual_privacy_review",
                    "docx_image_layout_and_text_association_not_preserved",
                }
            )
        # The raw DOCX remains a downloadable source copy, never a Gemini input.
        images.append(
            Media(
                filename,
                FORMATS[extension],
                raw,
                canonical({"transform": "original-docx", "original_sha256": sha256(raw)}),
                False,
            )
        )
        try:
            converted, origin, notes = render_docx(raw)
            origin["original_sha256"] = sha256(raw)
            visual, pdf_limits = pdf_media(filename + ".pdf", converted, origin)
            images.extend(visual)
            limits.update(notes)
            limits.update(pdf_limits)
            limits.update(
                {
                    "docx_rendering_fidelity_unverified",
                    "native_media_requires_visual_privacy_review",
                }
            )
        except (CollectionError, OSError, ValueError, ElementTree.ParseError) as exc:
            code = exc.code if isinstance(exc, CollectionError) else "docx_conversion_failed"
            if code == "docx_tracked_changes_requires_review":
                return replace(base, reason=code, limitations=tuple(sorted(limits)))
            limits.add("docx_visual_unprocessed:" + code)
        return replace(
            base,
            text=body.decode() if body is not None else None,
            media=tuple(images),
            reason=None
            if body or any(m.use_for_analysis for m in images)
            else "no_extractable_content",
            limitations=tuple(sorted(limits)),
        )
    except (CollectionError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        return replace(
            base, reason=exc.code if isinstance(exc, CollectionError) else "invalid_docx"
        )


class ExperienceInputs:
    def __init__(self, db: Database, materials: Materials):
        self.db, self.materials = db, materials

    def prepare(self, project: str) -> ExperienceInput:
        snapshot = AnalysisInput(self.db, self.materials).prepare(
            project, enforce_total_budget=False
        )
        return self.expand(snapshot)

    def expand(self, snapshot: InputSnapshot) -> ExperienceInput:
        entries: list[Evidence] = []
        prepared_media: dict[tuple[str, str], Evidence] = {}

        def prepare_material(reference: str, source: Row, raw: bytes) -> Evidence:
            # One conversion per identical filename/version in this frozen preparation.
            # Preserve every observation's source and avoid timestamp-dependent PDF
            # bytes separating identical content across completed Collections.
            key = (source["filename"], sha256(raw))
            if key not in prepared_media:
                prepared_media[key] = material_evidence(reference, source, raw)
            return replace(prepared_media[key], reference=reference, source_json=canonical(source))

        for entry in snapshot.entries:
            source = json.loads(entry.source_json)
            if source["kind"] == "user_material":
                try:
                    row, raw = self.materials.read(snapshot.project_id, source["material_id"])
                    if row["sha256"] != source["raw_sha256"]:
                        raise CollectionError(
                            "material_version_changed", "자료 버전이 변경됐습니다."
                        )
                    entries.append(prepare_material(entry.reference, source, raw))
                except (CollectionError, OSError) as exc:
                    entries.append(
                        Evidence(
                            entry.reference,
                            entry.source_json,
                            reason=exc.code
                            if isinstance(exc, CollectionError)
                            else "storage_unavailable",
                        )
                    )
            elif source["kind"] != "head_files":
                entries.append(
                    Evidence(
                        entry.reference,
                        entry.source_json,
                        entry.text,
                        reason=entry.reason,
                        limitations=entry.limitations,
                    )
                )
        # Read immutable historical objects, never replace them with current working files.
        # All reachable commits in every completed Collection are included, not only HEAD.
        project = self.db.one(
            "SELECT path,repository_key FROM projects WHERE id=%s", (snapshot.project_id,)
        )
        try:
            git, info = diagnose(project["path"])
            if project["repository_key"] != info["repository_key"]:
                raise CollectionError("repository_replaced", "등록 Repository가 변경됐습니다.")
            repository_reason = None
        except CollectionError as exc:
            git, repository_reason = None, exc.code
        tree_cache: dict[str, list[Row]] = {}
        blob_cache: dict[str, bytes] = {}
        resolved: dict[tuple[str, str, str, str], Evidence] = {}
        for entry in snapshot.entries:
            source = json.loads(entry.source_json)
            if source["kind"] not in {"commits", "head_files"}:
                continue
            cid = source["collection_id"]
            commit = source.get("oid") if source["kind"] == "commits" else None
            if commit is None:
                # HEAD observations remain in the manifest, including commitless/no-body cases.
                locator = source["locator"]
                matching = resolved.get(
                    (cid, locator.get("oid"), locator.get("path_b64"), locator.get("mode"))
                )
                if matching:
                    entries.append(
                        replace(
                            matching,
                            reference=entry.reference,
                            source_json=canonical(
                                {**source, "resolved_source": json.loads(matching.source_json)}
                            ),
                        )
                    )
                else:
                    entries.append(
                        Evidence(entry.reference, entry.source_json, reason=entry.reason)
                    )
                continue
            try:
                if git is None:
                    raise CollectionError(repository_reason or "git_unavailable", "원본 조회 불가")
                if commit not in tree_cache:
                    tree_cache[commit] = git.tree(commit)
                for file in tree_cache[commit]:
                    ref = f"{cid}/source/{commit}/{file['path_b64']}"
                    provenance = {
                        "kind": "historical_source",
                        "collection_id": cid,
                        "commit_oid": commit,
                        "locator": file,
                        "path_b64": file["path_b64"],
                        "read_at": datetime.now(UTC).isoformat(),
                        "provenance": "original_git_object_read_now",
                    }
                    reason = (
                        "sensitive_path"
                        if sensitive_path(file["path"])
                        else "symlink"
                        if file["mode"] == "120000"
                        else "submodule"
                        if file["mode"] == "160000"
                        else None
                    )
                    blob_raw: bytes | None = None
                    if not reason:
                        try:
                            if file["oid"] not in blob_cache:
                                size = int(git.object_read("cat-file", "-s", file["oid"]))
                                if size > MAX_MEDIA_BYTES:
                                    raise CollectionError(
                                        "source_size_limit", "원본 크기 제한 초과"
                                    )
                                blob_cache[file["oid"]] = git.object_read(
                                    "cat-file", "blob", file["oid"], limit=MAX_MEDIA_BYTES
                                )
                            blob_raw = blob_cache[file["oid"]]
                        except CollectionError as exc:
                            reason = exc.code
                    if (
                        blob_raw is not None
                        and Path(file["path"]).suffix.lower() in FORMATS.keys() - TEXT_FORMATS
                    ):
                        entries.append(
                            prepare_material(
                                ref, {**provenance, "filename": file["path"]}, blob_raw
                            )
                        )
                    else:
                        reason = reason or (text_reason(blob_raw) if blob_raw is not None else None)
                        entries.append(
                            Evidence(
                                ref,
                                canonical(provenance),
                                blob_raw.decode() if blob_raw is not None and not reason else None,
                                reason=reason,
                            )
                        )
                    resolved[(cid, file["oid"], file["path_b64"], file["mode"])] = entries[-1]
            except CollectionError as exc:
                entries.append(
                    Evidence(
                        f"{cid}/source/{commit}",
                        canonical(source),
                        reason=exc.code,
                        limitations=("historical_tree_unavailable",),
                    )
                )
        return ExperienceInput(
            snapshot.project_id, snapshot.prepared_at, snapshot.collections_json, tuple(entries)
        )
