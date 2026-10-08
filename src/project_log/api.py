import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_to_bytes
from uuid import UUID

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from project_log.browser import Browser, Category
from project_log.collections import Collections, Memo
from project_log.db import RECORDS, Database, Row
from project_log.git import CollectionError, diagnose
from project_log.materials import FORMATS, Materials
from project_log.service import Projects, Registration, Settings
from project_log.worker import Worker


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


def create_app(
    db: Database | None = None,
    *,
    start_worker: bool = True,
    storage_root: Path | None = None,
    material_max_bytes: int | None = None,
) -> FastAPI:
    database = db or Database()
    projects = Projects(database)
    browser = Browser(database)
    materials = Materials(database, storage_root, material_max_bytes)
    worker: Worker | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal worker
        database.migrate()
        if start_worker:
            worker = Worker(database)
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.close()

    app = FastAPI(title="Project Log", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        allowed = {
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        origin = request.headers.get("origin")
        if (origin and origin not in allowed) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            return JSONResponse(
                {"detail": "외부 웹사이트의 접근은 허용하지 않습니다."}, status_code=403
            )
        if request.method in {"POST", "PATCH"}:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON 요청이 필요합니다."}, status_code=415)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 16000:
                    return JSONResponse({"detail": "요청이 너무 큽니다."}, status_code=413)
            request._body = bytes(chunks)
        if (
            request.method == "PUT"
            and request.headers.get("content-type") != "application/octet-stream"
        ):
            return JSONResponse({"detail": "파일 bytes 요청이 필요합니다."}, status_code=415)
        if request.url.path.startswith("/api/collections/") and request.method == "GET":
            parts = request.url.path.split("/")
            try:
                cid = str(UUID(parts[3]))
            except ValueError:
                cid = None
            if cid and not await run_in_threadpool(
                database.all,
                "SELECT id FROM collections WHERE id=%s "
                "AND state IN ('completed','capturing','queued','running','cancel_pending')",
                (cid,),
            ):
                return JSONResponse({"detail": "수집 기록이 없습니다."}, status_code=404)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(CollectionError)
    async def collection_error(_request: Request, exc: CollectionError) -> JSONResponse:
        status = (
            404
            if exc.code == "not_found"
            else 409
            if exc.code in {"duplicate", "conflict", "filename_conflict", "content_duplicate"}
            else 413
            if exc.code == "file_too_large"
            else 503
            if exc.code in {"storage_failed", "storage_corrupt"}
            else 422
        )
        return JSONResponse({"code": exc.code, "detail": exc.message}, status_code=status)

    @app.exception_handler(psycopg.Error)
    async def database_error(_request: Request, _exc: psycopg.Error) -> JSONResponse:
        return JSONResponse(
            {
                "code": "database_unavailable",
                "detail": "DB 작업에 실패했습니다. 로컬 DB 상태를 확인하세요.",
            },
            status_code=503,
        )

    @app.get("/api/health")
    def health() -> Row:
        database.one("SELECT 1 AS ok")
        return {
            "database": "ready",
            "worker_error": worker.error if worker else None,
            "worker_running": bool(worker and worker.thread.is_alive()),
        }

    @app.post("/api/repositories/diagnose")
    def diagnosis(body: Diagnosis) -> Row:
        git, info = diagnose(body.path)
        info["suggested_base_branch"] = git.suggested_base_branch()
        info["duplicate"] = bool(
            database.all(
                "SELECT id FROM projects WHERE repository_key=%s", (info["repository_key"],)
            )
        )
        return info

    @app.get("/api/projects")
    def listing() -> list[Row]:
        return database.all("""SELECT p.id,p.name,p.path,p.repository_key,p.status,p.base_branch,
            p.repository_info,p.created_at,p.updated_at,
            c.id AS collection_id, c.state AS collection_state,
            c.summary AS collection_summary,
            EXISTS (SELECT 1 FROM collections a WHERE a.project_id=p.id
                    AND a.state<>'completed') AS collection_busy
            FROM projects p LEFT JOIN LATERAL
            (SELECT id,state,summary FROM collections WHERE project_id=p.id
                 AND state IN ('completed','capturing','queued','running','cancel_pending')
                 ORDER BY created_at DESC,id DESC LIMIT 1)
            c ON true ORDER BY p.created_at DESC""")

    @app.post("/api/projects", status_code=201)
    def register(body: Registration) -> Row:
        materials.check_repository(body.path)
        return projects.register(body)

    @app.get("/api/material-policy")
    def material_policy() -> Row:
        return {"extensions": list(FORMATS), "max_bytes": materials.max_bytes}

    @app.get("/api/projects/{project_id}/materials")
    def material_list(project_id: UUID) -> list[Row]:
        return materials.listing(str(project_id))

    @app.put("/api/projects/{project_id}/materials", status_code=201)
    async def add_material(project_id: UUID, request: Request) -> Row:
        try:
            filename = unquote_to_bytes(request.headers.get("x-file-name", "")).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CollectionError("invalid_filename", "파일명을 확인하세요.") from exc
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > materials.max_bytes:
                raise CollectionError("file_too_large", "파일 크기 제한을 초과했습니다.")
            raw.extend(chunk)
        try:
            return await run_in_threadpool(materials.add, str(project_id), filename, bytes(raw))
        except OSError as exc:
            raise CollectionError(
                "storage_failed",
                "보관 사본 저장에 실패했습니다. 로컬 저장 영역과 여유 공간을 확인하세요.",
            ) from exc

    @app.get("/api/projects/{project_id}/materials/{material_id}/file")
    def material_file(project_id: UUID, material_id: UUID) -> Response:
        try:
            row, raw = materials.read(str(project_id), str(material_id))
        except OSError as exc:
            raise CollectionError(
                "storage_failed", "보관 사본을 읽을 수 없습니다. 로컬 저장 영역을 확인하세요."
            ) from exc
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename=material; filename*=UTF-8''"
                + quote(row["filename"], safe=""),
            },
        )

    @app.delete("/api/projects/{project_id}/materials/{material_id}")
    def delete_material(project_id: UUID, material_id: UUID) -> Row:
        try:
            materials.delete(str(project_id), str(material_id))
        except OSError as exc:
            raise CollectionError(
                "storage_failed", "자료 삭제에 실패했습니다. 로컬 저장 영역을 확인하세요."
            ) from exc
        return {"deleted": str(material_id)}

    @app.get("/api/projects/{project_id}")
    def project(project_id: UUID) -> Row:
        return projects.project(str(project_id))

    @app.patch("/api/projects/{project_id}")
    def update(project_id: UUID, body: Settings) -> Row:
        return projects.update(str(project_id), body)

    @app.post("/api/projects/{project_id}/collections", status_code=202)
    def collect_now(project_id: UUID) -> Row | None:
        return projects.collect_now(str(project_id))

    @app.get("/api/collections/{collection_id}")
    def collection(collection_id: UUID) -> Row:
        return database.detail(str(collection_id))

    @app.post("/api/collections/{collection_id}/cancel", status_code=202)
    def cancel(collection_id: UUID) -> Row:
        return Collections(database).cancel(str(collection_id))

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: UUID) -> Row:
        Collections(database).remove(str(collection_id))
        return {"deleted": str(collection_id)}

    @app.patch("/api/collections/{collection_id}")
    def memo(collection_id: UUID, body: Memo) -> Row:
        return Collections(database).memo(str(collection_id), body)

    @app.get("/api/collections/{collection_id}/records/{kind}")
    def records(
        collection_id: UUID,
        kind: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[Row]:
        columns = {
            "commits": "oid,metadata,body_reason,observed_at",
            "changes": "id,commit_oid,parent_oid,metadata,body_reason,observed_at",
            "head_files": "id,metadata,observed_at",
            "working_entries": "id,layer,metadata,sha256,body_reason,observed_at,"
            "content_id,body_observed_at,provenance,original_body_reason,repaired,repair_read_metadata",
        }
        if kind not in columns:
            raise CollectionError("not_found", "해당 자료 종류가 없습니다.")
        database.detail(str(collection_id))
        order = "oid" if kind == "commits" else "id"
        return database.all(
            f"SELECT {columns[kind]} FROM {RECORDS[kind]} WHERE collection_id=%s "
            f"ORDER BY {order} LIMIT %s OFFSET %s",
            (str(collection_id), limit, offset),
        )

    @app.get("/api/collections/{collection_id}/source")
    def source(
        collection_id: UUID,
        commit: str = Query(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$"),
        path_b64: str = Query(max_length=16000),
    ) -> Row:
        return projects.source(str(collection_id), commit, path_b64)

    @app.get("/api/collections/{collection_id}/overview")
    def overview(collection_id: UUID) -> Row:
        return browser.overview(str(collection_id))

    @app.get("/api/collections/{collection_id}/browse/{category}")
    def browse(
        collection_id: UUID,
        category: Category,
        directory_b64: str = Query("", max_length=16000),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> Row:
        return browser.directory(str(collection_id), category, directory_b64, limit, offset)

    @app.get("/api/collections/{collection_id}/commits")
    def commits(
        collection_id: UUID,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> Row:
        return browser.commits(str(collection_id), limit, offset)

    @app.get("/api/collections/{collection_id}/commits/{commit}")
    def commit_detail(collection_id: UUID, commit: str) -> Row:
        return database.one(
            "SELECT oid,metadata,body_reason,observed_at FROM commit_records "
            "WHERE collection_id=%s AND oid=%s",
            (str(collection_id), commit),
        )

    @app.get("/api/collections/{collection_id}/commits/{commit}/changes")
    def commit_changes(
        collection_id: UUID,
        commit: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> Row:
        return browser.changes(str(collection_id), commit, limit, offset)

    @app.get("/api/collections/{collection_id}/content/{kind}/{record_id}")
    def content(collection_id: UUID, kind: str, record_id: int) -> Row:
        column = {"working_entries": "body", "changes": "diff"}.get(kind)
        if column is None:
            raise CollectionError("not_found", "해당 본문 종류가 없습니다.")
        row = database.one(
            f"SELECT {column},body_reason FROM {RECORDS[kind]} WHERE collection_id=%s AND id=%s",
            (str(collection_id), record_id),
        )
        body = row.pop(column)
        return {"body": bytes(body).decode() if body is not None else None, **row}

    dist = Path(
        os.environ.get(
            "PROJECT_LOG_FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend/dist")
        )
    )
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True))
    return app
