"""Project-owned original copies, independent of Collection contents and source paths."""

import hashlib
import io
import os
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from project_log.db import Database, Row
from project_log.git import CollectionError

FORMATS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
TEXT_FORMATS = {".txt", ".md", ".log", ".json", ".csv"}
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def default_storage_root() -> Path:
    configured = os.environ.get("PROJECT_LOG_STORAGE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    if not base.is_absolute():
        raise ValueError("XDG_DATA_HOME must be absolute")
    return (base / "project-log").resolve()


def filename_key(filename: str) -> str:
    # Portable leaf names; paths/folders and Windows-invalid/control names are rejected.
    normalized = unicodedata.normalize("NFKC", filename)
    if (
        not filename
        or len(filename) > 255
        or len(filename.encode("utf-8")) > 255
        or any(c in normalized for c in '/\\<>:"|?*')
        or any(unicodedata.category(c).startswith("C") for c in normalized)
        or normalized in {".", ".."}
        or normalized != normalized.strip()
        or normalized.endswith(".")
        or normalized.split(".")[0].casefold()
        in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }
    ):
        raise CollectionError(
            "invalid_filename", "폴더·경로는 지원하지 않습니다. 파일명을 확인하세요."
        )
    return unicodedata.normalize("NFKC", normalized.casefold())


def validate_format(filename: str, raw: bytes) -> tuple[str, str]:
    extension = Path(filename).suffix.lower()
    if extension not in FORMATS:
        raise CollectionError("unsupported_format", "지원하지 않는 파일 형식입니다.")
    valid = False
    if extension in TEXT_FORMATS:
        try:
            raw.decode("utf-8-sig")
            valid = b"\0" not in raw
        except UnicodeDecodeError:
            pass
    elif extension == ".pdf":
        valid = raw.startswith(b"%PDF-")
    elif extension == ".png":
        valid = raw.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = raw.startswith(b"\xff\xd8\xff")
    elif extension == ".docx":
        try:
            # Inspect ZIP directory only. Do not decompress, extract, or parse document text.
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                valid = {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist())
        except (zipfile.BadZipFile, ValueError):
            pass
    if not valid:
        raise CollectionError(
            "invalid_format", "파일 내용이 지원 형식과 맞지 않습니다. 텍스트는 UTF-8이 필요합니다."
        )
    return extension, FORMATS[extension]


class Materials:
    def __init__(self, db: Database, root: Path | None = None, max_bytes: int | None = None):
        self.db = db
        self.root = (root or default_storage_root()).resolve()
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else int(os.environ.get("PROJECT_LOG_MATERIAL_MAX_BYTES", DEFAULT_MAX_BYTES))
        )
        if self.max_bytes <= 0:
            raise ValueError("Material size limit must be positive")

    def check_repository(self, repository: str) -> None:
        repository_root = Path(repository).expanduser().resolve()
        if self.root.is_relative_to(repository_root) or repository_root.is_relative_to(self.root):
            raise CollectionError(
                "storage_in_repository",
                "자료 저장 영역은 등록 Repository 밖에 있어야 합니다. 저장 설정을 확인하세요.",
            )

    def path(self, project: str, material: str) -> Path:
        return self.root / "materials" / str(UUID(project)) / str(UUID(material))

    def _directory(self, directory: Path, *, create: bool = False) -> None:
        # Managed children must never redirect operations into original/user repositories.
        if directory.is_symlink():
            raise OSError("Managed storage directory is a symlink")
        if create:
            directory.mkdir(parents=directory == self.root, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise OSError("Managed storage directory is unavailable")

    def _target(self, project: str, material: str, *, create: bool = False) -> Path:
        target = self.path(project, material)
        for directory in (self.root, self.root / "materials", target.parent):
            self._directory(directory, create=create)
        if target.is_symlink():
            raise OSError("Managed copy is a symlink")
        return target

    def listing(self, project: str) -> list[Row]:
        self.db.one("SELECT id FROM projects WHERE id=%s", (project,))
        return self.db.all(
            "SELECT * FROM user_materials WHERE project_id=%s ORDER BY added_at DESC,id DESC",
            (project,),
        )

    def _stage(self, raw: bytes) -> Path:
        self._directory(self.root, create=True)
        directory = self.root / ".tmp"
        self._directory(directory, create=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            staged = Path(stream.name)
            try:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        return staged

    def add(self, project: str, filename: str, raw: bytes) -> Row:
        key = filename_key(filename)
        if len(raw) > self.max_bytes:
            raise CollectionError("file_too_large", "파일 크기 제한을 초과했습니다.")
        extension, media_type = validate_format(filename, raw)
        digest = hashlib.sha256(raw).hexdigest()
        material = str(uuid4())
        target = self.path(project, material)
        staged: Path | None = None
        moved = False
        try:
            with self.db.connect() as conn:
                if not conn.execute(
                    "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)
                ).fetchone():
                    raise CollectionError("not_found", "해당 Project가 없습니다.")
                for repository in conn.execute("SELECT path FROM projects").fetchall():
                    self.check_repository(repository["path"])
                same_name = conn.execute(
                    "SELECT sha256 FROM user_materials WHERE project_id=%s AND filename_key=%s",
                    (project, key),
                ).fetchone()
                if same_name and same_name["sha256"] != digest:
                    raise CollectionError(
                        "filename_conflict",
                        "같은 파일명이 있습니다. 원본 파일명을 변경한 뒤 다시 업로드하세요.",
                    )
                if (
                    same_name
                    or conn.execute(
                        "SELECT id FROM user_materials WHERE project_id=%s AND sha256=%s",
                        (project, digest),
                    ).fetchone()
                ):
                    raise CollectionError(
                        "content_duplicate", "동일한 내용의 자료가 이미 있습니다."
                    )
                staged = self._stage(raw)
                self._target(project, material, create=True)
                os.replace(staged, target)
                moved = True
                row = conn.execute(
                    """INSERT INTO user_materials
                    (id,project_id,filename,filename_key,size_bytes,extension,media_type,sha256)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (material, project, filename, key, len(raw), extension, media_type, digest),
                ).fetchone()
                assert row is not None
            return row
        except BaseException:
            if moved:
                target.unlink(missing_ok=True)
            raise
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    def read(self, project: str, material: str) -> tuple[Row, bytes]:
        # Lock against delete until bytes have been read; never open an external source path.
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_materials WHERE project_id=%s AND id=%s FOR SHARE",
                (project, material),
            ).fetchone()
            if row is None:
                raise CollectionError("not_found", "해당 사용자 추가 자료가 없습니다.")
            target = self._target(project, material)
            with target.open("rb") as stream:
                raw = stream.read()
            if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise CollectionError("storage_corrupt", "보관 사본의 무결성을 확인할 수 없습니다.")
            return row, raw

    def delete(self, project: str, material: str) -> None:
        target = self.path(project, material)
        raw: bytes | None = None
        removed = False
        try:
            with self.db.connect() as conn:
                conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,))
                row = conn.execute(
                    "SELECT id FROM user_materials WHERE project_id=%s AND id=%s FOR UPDATE",
                    (project, material),
                ).fetchone()
                if row is None:
                    raise CollectionError("not_found", "해당 사용자 추가 자료가 없습니다.")
                # Keep bytes during this short transaction so DB failure can restore the copy.
                self._target(project, material)
                raw = target.read_bytes()
                conn.execute(
                    "DELETE FROM user_materials WHERE project_id=%s AND id=%s", (project, material)
                )
                target.unlink()
                removed = True
        except BaseException:
            if removed and raw is not None:
                staged = self._stage(raw)
                try:
                    os.replace(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
            raise
