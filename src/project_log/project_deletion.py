"""Project-owned deletion with a durable, short-lived storage rollback record."""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from project_log.db import Database, Row
from project_log.git import CollectionError
from project_log.materials import Materials


class DeleteProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class ProjectDeletion:
    def __init__(self, db: Database, materials: Materials):
        self.db, self.materials = db, materials
        self.root = materials.root / ".project-deletions"

    @staticmethod
    def _sync(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _directory(self, path: Path, create: bool = False) -> None:
        self.materials._directory(path, create=create)

    @staticmethod
    def _raw(path: Path, row: Row) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise OSError("Invalid managed copy")
        raw = path.read_bytes()
        if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise OSError("Managed copy integrity mismatch")
        return raw

    def _manifest(self, project: str) -> Row:
        stage = self.root / project
        for directory in (self.materials.root, self.root, stage):
            self._directory(directory)
        path = stage / "manifest.json"
        if path.is_symlink():
            raise OSError("Invalid deletion record")
        record: Row = json.loads(path.read_text())
        if record["project"] != project:
            raise OSError("Invalid deletion identity")
        for item in record["materials"]:
            if str(UUID(item["id"])) != item["id"]:
                raise OSError("Invalid material identity")
        expected = {"manifest.json", *(r["id"] for r in record["materials"])}
        if any(p.name not in expected for p in stage.iterdir()):
            raise OSError("Unexpected deletion storage entry")
        return record

    def _finish(self, project: str, record: Row, *, restore: bool) -> None:
        stage = self.root / project
        for row in record["materials"]:
            backup, target = stage / row["id"], self.materials.path(project, row["id"])
            if restore:
                self.materials._target(project, row["id"], create=True)
                if target.exists():
                    self._raw(target, row)
                else:
                    self._raw(backup, row)
                    os.replace(backup, target)
                    self._sync(target.parent)
            if backup.exists() or backup.is_symlink():
                if not restore:
                    self._raw(backup, row)
                elif backup.is_symlink():
                    raise OSError("Invalid staged copy")
                backup.unlink()
        self._sync(stage)
        if not restore:
            directory = self.materials.root / "materials" / project
            if directory.exists() or directory.is_symlink():
                self._directory(directory)
                directory.rmdir()  # Never recursively remove an unexpected file/directory.
                self._sync(directory.parent)
        (stage / "manifest.json").unlink()
        stage.rmdir()
        self._sync(self.root)

    def recover(self) -> None:
        # No storage writes occur on ordinary startup. Only interrupted deletion records
        # are reconciled, before the worker/API starts serving requests.
        if not self.root.exists() and not self.root.is_symlink():
            return
        self._directory(self.root)
        for stage in self.root.iterdir():
            project = str(UUID(stage.name))
            with self.db.connect() as conn:
                conn.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s,0))", (f"delete:{project}",)
                )
                exists = conn.execute(
                    "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)
                ).fetchone()
                if not (stage / "manifest.json").exists():
                    self._directory(stage)
                    if any(p.name != ".manifest.tmp" or p.is_symlink() for p in stage.iterdir()):
                        raise OSError("Invalid unfinished deletion record")
                    (stage / ".manifest.tmp").unlink(missing_ok=True)
                    stage.rmdir()
                    self._sync(self.root)
                else:
                    self._finish(project, self._manifest(project), restore=bool(exists))

    def _prepare(self, project: str, name: str, rows: list[Row]) -> Row:
        self._directory(self.materials.root, create=True)
        self._directory(self.root, create=True)
        stage = self.root / project
        self._directory(stage, create=True)
        record: Row = {"project": project, "name": name, "materials": rows}
        try:
            directory = self.materials.root / "materials" / project
            if directory.exists() or directory.is_symlink():
                self._directory(directory)
                if {p.name for p in directory.iterdir()} != {r["id"] for r in rows}:
                    raise OSError("Unexpected project storage entry")
            with (stage / ".manifest.tmp").open("x") as stream:
                os.chmod(stream.name, 0o600)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage / ".manifest.tmp", stage / "manifest.json")
            self._sync(stage)
            self._sync(self.root)
            for row in rows:
                target = self.materials._target(project, row["id"])
                raw = self._raw(target, row)
                with (stage / row["id"]).open("xb") as stream:
                    os.chmod(stream.name, 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._sync(stage)
            self._sync(self.root)
            return record
        except BaseException:
            # Originals are still intact; remove only this operation's known staged files.
            for leaf in [*(r["id"] for r in rows), "manifest.json", ".manifest.tmp"]:
                (stage / leaf).unlink(missing_ok=True)
            stage.rmdir()
            raise

    @staticmethod
    def _records(conn: psycopg.Connection[Row], project: str) -> None:
        # Composite FKs scope shared bodies/objects to a Project. A cross-Project
        # baseline/retry link is not modified; its presence blocks deletion instead.
        conn.execute(
            "UPDATE collections SET baseline_id=NULL,retry_of=NULL WHERE project_id=%s", (project,)
        )
        for table in ("collection_issues", "collection_repairs"):
            conn.execute(
                f"DELETE FROM {table} WHERE collection_id IN "
                "(SELECT id FROM collections WHERE project_id=%s)",
                (project,),
            )
        for table in (
            "working_repairs",
            "changes",
            "head_files",
            "working_entries",
            "commits",
            "collections",
            "git_changes",
            "git_files",
            "git_commits",
            "contents",
            "user_materials",
            "projects",
        ):
            column = "id" if table == "projects" else "project_id"
            conn.execute(f"DELETE FROM {table} WHERE {column}=%s", (project,))

    def delete(self, project: str, name: str) -> None:
        project = str(UUID(project))
        record = None
        committed = False
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (f"delete:{project}",))
            owner = conn.execute(
                "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project,)
            ).fetchone()
            stage = self.root / project
            if stage.exists() or stage.is_symlink():
                previous = self._manifest(project)
                if previous["name"] != name:
                    raise CollectionError("conflict", "삭제할 프로젝트명을 정확히 입력하세요.")
                self._finish(project, previous, restore=bool(owner))
                if not owner:
                    return
            if not owner:
                raise CollectionError("not_found", "해당 프로젝트가 없습니다.")
            if owner["name"] != name:
                raise CollectionError(
                    "conflict", "프로젝트명이 변경됐거나 확인 입력이 일치하지 않습니다."
                )
            collections = conn.execute(
                "SELECT id,state FROM collections WHERE project_id=%s FOR UPDATE", (project,)
            ).fetchall()
            if any(c["state"] != "completed" for c in collections):
                raise CollectionError(
                    "conflict",
                    "수집·취소·실패 자료 정리가 진행 중인 프로젝트는 삭제할 수 없습니다.",
                )
            if conn.execute(
                """SELECT id FROM collections WHERE project_id<>%s AND
                (baseline_id IN (SELECT id FROM collections WHERE project_id=%s)
                 OR retry_of IN (SELECT id FROM collections WHERE project_id=%s))""",
                (project, project, project),
            ).fetchone():
                raise CollectionError(
                    "conflict", "다른 프로젝트에서 참조하는 수집 기록이 있어 삭제할 수 없습니다."
                )
            for repo in conn.execute("SELECT path FROM projects").fetchall():
                self.materials.check_repository(repo["path"])
            rows = conn.execute(
                "SELECT id,size_bytes,sha256 FROM user_materials WHERE project_id=%s FOR UPDATE",
                (project,),
            ).fetchall()
            rows = [{**row, "id": str(row["id"])} for row in rows]
            try:
                record = self._prepare(project, name, rows)
                self._records(conn, project)
                for row in rows:
                    self.materials._target(project, row["id"]).unlink()
                if rows:
                    self._sync(self.materials.path(project, rows[0]["id"]).parent)
                conn.commit()
                committed = True
                self._finish(project, record, restore=False)
            except BaseException as exc:
                conn.rollback()
                # If connection/commit outcome is unknown, read the DB again before
                # deciding to restore. Never guess that a failed COMMIT rolled back.
                if record and not committed:
                    exists = self.db.all("SELECT id FROM projects WHERE id=%s", (project,))
                    self._finish(project, record, restore=bool(exists))
                if committed:
                    raise CollectionError(
                        "storage_failed",
                        "프로젝트 등록 정보와 수집 기록은 삭제됐지만 보관 사본 정리가 "
                        "완료되지 않았습니다. 저장소를 확인한 뒤 이 삭제를 다시 시도하세요.",
                    ) from exc
                raise
