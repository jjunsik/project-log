import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from project_log.collector import capture, now
from project_log.db import CollectionStopped, Database, Row
from project_log.git import CollectionError, Git, diagnose
from project_log.policy import POLICY

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ProjectStatus = Literal["new", "ongoing", "completed"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    status: ProjectStatus
    base_branch: str = Field(max_length=4096)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    acknowledged_warnings: str | None = None
    expected_updated_at: datetime | None = None


class Registration(Settings):
    path: str = Field(min_length=1, max_length=4096)
    collect: bool = True


class Projects:
    def __init__(self, db: Database):
        self.db = db

    def register(self, request: Registration) -> Row:
        git, info = diagnose(request.path)
        if self.db.all(
            "SELECT id FROM projects WHERE repository_key=%s", (info["repository_key"],)
        ):
            raise CollectionError("duplicate", "이미 등록된 Repository입니다.")
        if request.collect:
            info.update(git.collection_branch(request.base_branch))
        else:
            git.validate_base_branch(request.base_branch)
        project, collection = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            with self.db.connect() as conn:
                # Preserve the legacy NOT NULL column without exposing a Product setting.
                conn.execute(
                    """INSERT INTO projects
                    (id,name,path,repository_key,status,base_branch,coding_agent,repository_info)
                    VALUES (%s,%s,%s,%s,%s,%s,'none',%s)""",
                    (
                        project,
                        request.name,
                        info["path"],
                        info["repository_key"],
                        request.status,
                        request.base_branch,
                        Jsonb(info),
                    ),
                )
                if request.collect:
                    conn.execute(
                        """INSERT INTO collections(id,project_id,state,policy)
                    VALUES (%s,%s,'capturing',%s)""",
                        (collection, project, Jsonb(POLICY)),
                    )
        except psycopg.errors.UniqueViolation as exc:
            raise CollectionError("duplicate", "이미 등록된 Repository입니다.") from exc
        if request.collect:
            self._capture(collection, git, info, request.base_branch)
        return self.project(project)

    def _capture(self, collection: str, git: Git, info: Row, base_branch: str) -> None:
        try:
            capture(self.db, collection, git, info, expected_branch=base_branch)
        except CollectionStopped:
            pass
        except Exception:
            self.db.mark_cleanup(collection)

    def collect_now(self, project: str) -> Row | None:
        collection = str(uuid.uuid4())
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project,)
                ).fetchone()
                if row is None:
                    raise CollectionError("not_found", "프로젝트가 없습니다.")
                if conn.execute(
                    """SELECT 1 FROM collections WHERE project_id=%s
                    AND state<>'completed'""",
                    (project,),
                ).fetchone():
                    raise CollectionError("conflict", "이미 진행 중인 수집이 있습니다.")
                git, info = diagnose(row["path"])
                if info["repository_key"] != row["repository_key"]:
                    raise CollectionError("repository_replaced", "등록 Repository가 변경됐습니다.")
                info.update(git.collection_branch(row["base_branch"]))
                previous = conn.execute(
                    """SELECT id FROM collections WHERE project_id=%s
                    AND kind<>'retry' AND state='completed'
                    ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (project,),
                ).fetchone()
                conn.execute(
                    """INSERT INTO collections(id,project_id,kind,baseline_id,state,policy)
                    VALUES (%s,%s,'manual',%s,'capturing',%s)""",
                    (collection, project, previous["id"] if previous else None, Jsonb(POLICY)),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise CollectionError("conflict", "이미 진행 중인 수집이 있습니다.") from exc
        self._capture(collection, git, info, row["base_branch"])
        rows = self.db.all(
            "SELECT * FROM collections WHERE id=%s AND state "
            "IN ('capturing','queued','running','completed','cancel_pending')",
            (collection,),
        )
        if not rows:
            return None
        result = rows[0]
        result["issues"] = self.db.all(
            "SELECT * FROM collection_issues WHERE collection_id=%s ORDER BY id", (collection,)
        )
        return result

    def project(self, project: str) -> Row:
        row = self.db.one(
            """SELECT id,name,path,repository_key,status,base_branch,
            repository_info,created_at,updated_at
            FROM projects WHERE id=%s""",
            (project,),
        )
        row["collections"] = self.db.all(
            "SELECT * FROM collections WHERE project_id=%s "
            "AND state IN ('completed','capturing','queued','running','cancel_pending') "
            "ORDER BY created_at DESC,id DESC",
            (project,),
        )
        row["collection_busy"] = bool(
            self.db.all(
                "SELECT 1 FROM collections WHERE project_id=%s AND state<>'completed'", (project,)
            )
        )
        return row

    def update(self, project: str, settings: Settings) -> Row:
        from project_log.materials import Materials
        from project_log.reconnection import review

        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project,)
            ).fetchone()
            if row is None:
                raise CollectionError("not_found", "프로젝트가 없습니다.")
            if (
                settings.expected_updated_at is not None
                and row["updated_at"] != settings.expected_updated_at
            ):
                raise CollectionError(
                    "conflict",
                    "다른 작업에서 설정이 변경됐습니다. 현재 저장값을 확인한 뒤 다시 저장하세요.",
                )
            path_changed = settings.path is not None and settings.path != row["path"]
            if settings.base_branch != row["base_branch"] or path_changed:
                if conn.execute(
                    "SELECT 1 FROM collections WHERE project_id=%s AND state<>'completed'",
                    (project,),
                ).fetchone():
                    raise CollectionError("conflict", "수집이 끝난 뒤 기준 브랜치를 변경하세요.")
                git, info = diagnose(settings.path or row["path"])
                if not path_changed and info["repository_key"] != row["repository_key"]:
                    raise CollectionError("repository_replaced", "등록 Repository가 변경됐습니다.")
                git.validate_base_branch(settings.base_branch)
                if path_changed:
                    Materials(self.db).check_repository(info["path"])
                    result = review(conn, row, git, info, settings.base_branch)
                    if result["warning_token"] != settings.acknowledged_warnings:
                        raise CollectionError(
                            "confirmation_required", "조회 제한이 변경됐습니다. 다시 확인하세요."
                        )
                    # Repeat the locator/refs observation immediately before the UPDATE.
                    _, fresh = diagnose(info["path"])
                    if fresh != info or git.refs() != result["refs"]:
                        raise CollectionError(
                            "repository_changed",
                            "검증 중 Repository가 변경됐습니다. 다시 확인하세요.",
                        )
                    row["path"], row["repository_key"] = info["path"], info["repository_key"]
            conn.execute(
                """UPDATE projects SET name=%s,status=%s,base_branch=%s,path=%s,repository_key=%s,
                updated_at=now() WHERE id=%s""",
                (
                    settings.name,
                    settings.status,
                    settings.base_branch,
                    row["path"],
                    row["repository_key"],
                    project,
                ),
            )
        return self.project(project)

    def review_path(self, project: str, settings: Settings) -> Row:
        from project_log.materials import Materials
        from project_log.reconnection import review

        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project,)
            ).fetchone()
            if row is None:
                raise CollectionError("not_found", "프로젝트가 없습니다.")
            if conn.execute(
                "SELECT 1 FROM collections WHERE project_id=%s AND state<>'completed'", (project,)
            ).fetchone():
                raise CollectionError("conflict", "수집·정리가 끝난 뒤 경로를 변경하세요.")
            git, info = diagnose(settings.path or row["path"])
            Materials(self.db).check_repository(info["path"])
            git.validate_base_branch(settings.base_branch)
            return review(conn, row, git, info, settings.base_branch)

    def source(self, collection: str, commit: str, path_b64: str) -> dict[str, Any]:
        row = self.db.one(
            """SELECT p.path,p.repository_key FROM collections c
            JOIN projects p ON p.id=c.project_id JOIN commits m ON m.collection_id=c.id
            WHERE c.id=%s AND m.oid=%s""",
            (collection, commit),
        )
        git, info = diagnose(row["path"])
        if row["repository_key"] != info["repository_key"]:
            raise CollectionError("repository_replaced", "등록 Repository가 변경됐습니다.")
        for file in git.tree(commit):
            if file["path_b64"] == path_b64:
                body, reason, size = git.blob(file["oid"], file["path"], file["mode"])
                return {
                    **file,
                    "body": body.decode() if body is not None else None,
                    "body_reason": reason,
                    "size": size,
                    "commit": commit,
                    "provenance": "original_git_object_read_now",
                    "read_at": now(),
                    "collection_id": collection,
                }
        raise CollectionError("not_found", "해당 Commit에 파일이 없습니다.")
