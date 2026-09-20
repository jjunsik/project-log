import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from project_log.analysis import ProviderUnavailable
from project_log.models import (
    EvaluationRequest,
    Golden,
    GoldenApproval,
    ReviewRequest,
    RunRequest,
    TimingSample,
)
from project_log.service import Conflict, Pilot


def create_app(root: Path | None = None, data_root: Path | None = None) -> FastAPI:
    root = root or Path(__file__).resolve().parents[2]
    configured_data = os.environ.get("PROJECT_LOG_DATA_DIR")
    pilot = Pilot(
        root, data_root or (Path(configured_data) if configured_data else root / ".local/pilot")
    )
    app = FastAPI(title="Project Log Pilot", docs_url=None, redoc_url=None)
    app.state.pilot = pilot
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next: Any) -> Any:
        allowed = {
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        if request.headers.get("origin") and request.headers["origin"] not in allowed:
            return JSONResponse({"detail": "Untrusted origin"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON required"}, status_code=415)
            if len(await request.body()) > 200_000:
                return JSONResponse({"detail": "Pilot request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc).split("\n")[0]}, status_code=422)

    @app.exception_handler(FileNotFoundError)
    async def missing(_request: Request, _exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": "Record not found in this case"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ProviderUnavailable)
    async def unavailable(_request: Request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=412)

    @app.get("/api/readiness")
    def readiness() -> dict[str, Any]:
        return {
            "mode": "offline-first",
            "credential_present": bool(os.environ.get("GEMINI_API_KEY")),
            "live_policy_present": (root / ".local/gemini-policy.json").is_file(),
            "gate": "UNSET",
            "storage": "local immutable JSON",
        }

    @app.get("/api/cases")
    def cases() -> list[dict[str, Any]]:
        return [
            {"id": c.id, "title": c.title, "purpose": c.purpose, "project_key": c.project_key}
            for c in pilot.cases.values()
        ]

    @app.get("/api/cases/{case_id}")
    def state(case_id: str) -> dict[str, Any]:
        return pilot.state(case_id)

    @app.post("/api/cases/{case_id}/runs")
    def run(case_id: str, request: RunRequest) -> dict[str, Any]:
        return pilot.run(case_id, request)

    @app.post("/api/cases/{case_id}/reviews")
    def review(case_id: str, request: ReviewRequest) -> dict[str, Any]:
        return pilot.review(case_id, request)

    @app.post("/api/cases/{case_id}/goldens")
    def candidate(case_id: str, request: Golden) -> dict[str, Any]:
        return pilot.save_golden(case_id, request)

    @app.post("/api/cases/{case_id}/golden-approvals")
    def approve(case_id: str, request: GoldenApproval) -> dict[str, Any]:
        return pilot.approve_golden(case_id, request)

    @app.post("/api/cases/{case_id}/evaluations")
    def evaluation(case_id: str, request: EvaluationRequest) -> dict[str, Any]:
        return pilot.evaluate(case_id, request)

    @app.post("/api/cases/{case_id}/timing")
    def timing(case_id: str, request: TimingSample) -> dict[str, Any]:
        return pilot.timing(case_id, request)

    @app.get("/api/cases/{case_id}/export")
    def export(case_id: str) -> dict[str, Any]:
        return pilot.export(case_id)

    if (root / "frontend/dist").is_dir():
        app.mount("/", StaticFiles(directory=root / "frontend/dist", html=True), name="ui")
    return app
