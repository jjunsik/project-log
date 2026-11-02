"""Admission projections and no-store, local-only previews of fixed transfer inputs."""

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from project_log.analysis_admission import AnalysisAdmission, PrepareAnalysis, SubmitAnalysis
from project_log.analysis_diagnostic import validate_target
from project_log.analysis_events import DURABILITY_POLICY, validated_candidates
from project_log.analysis_execution import execution_profile
from project_log.analysis_recovery import changed_request_resume, daily_reset, resume_block
from project_log.analysis_worker import AnalysisWorker
from project_log.db import Database, Row
from project_log.gemini import REVIEWED_MODELS, operator_secret_directory
from project_log.git import CollectionError

TERMS = "https://ai.google.dev/gemini-api/terms"
TRANSFER = (
    "선택 범위의 보존 텍스트·코드·diff·문서와 출처 정보, "
    "처리된 PDF·이미지, 분석 지시를 전송합니다. "
    "수집·Git 원본 조회·AI 입력과 출력에서 개인정보·민감정보·Secret을 자동 판별하거나 "
    "마스킹·가명 처리하지 않습니다. 서비스 Secret 전용 폴더는 수집에서 제외됩니다. "
    "사용자가 전체 전송 자료·제외/처리 한계와 제공 권한을 직접 확인해야 합니다. "
    "전체 전송 재료 JSON과 각 비텍스트 자료를 준비 화면에서 확인·다운로드할 수 있습니다. "
    "분할·중복 통합된 자료가 단계별 호출에 사용되며 "
    "후속 문맥에는 이번 실행의 AI 응답도 포함됩니다. "
    "제외된 본문과 DOCX 원본은 전송하지 않습니다. "
    "DOCX 추출 텍스트·파생 PDF/이미지는 포함될 수 있습니다."
)
POLICY = (
    "Google Gemini Developer API의 Unpaid Services 정책을 적용합니다. "
    "입력·응답은 Google 제품과 ML 개선에 사용되고 사람이 검토할 수 있습니다. "
    "민감·기밀·개인정보를 전송하면 안 됩니다. "
    "자료의 민감성 자동 판별은 제공하지 않습니다. 사용자가 제공 권한과 "
    "PDF·이미지 내부 정보까지 직접 확인해야 합니다. "
    "사용자 확인이 서비스의 법적·기술적 책임을 모두 면제하지는 않습니다. "
    "Free tier는 운영자 확인이며 실제 Billing·비용을 "
    "증명하지 않습니다. 이미 시작된 외부 호출의 즉시 중단·과금 취소는 보장하지 않습니다. "
    f"정책 확인: {TERMS} (안내 버전 2026-10-28)"
)


def public_profile(profile: Row) -> Row:
    isolated = profile["mode"] == "isolated"
    preparation_only = profile.get("preparation_only", False)
    return {
        **profile,
        "provider": "격리 합성 모델 · 외부 전송 없음"
        if isolated
        else "Google Gemini Developer API",
        "transfer_scope": "격리 서버 내부의 합성 입력만 사용합니다. 외부 AI 전송은 없습니다."
        if isolated
        else TRANSFER,
        "data_policy": "합성·Mock 실행이며 실제 Gemini 분석 품질 검증이 아닙니다."
        if isolated
        else (
            "로컬 준비 전용: 분석 접수·실행·외부 전송이 차단돼 있습니다. "
            "Key 프로젝트·권한·무료 한도는 미확인입니다. 향후 전송 시 적용할 정책: "
            if preparation_only
            else ""
        )
        + POLICY,
        "notice_version": "analysis-preparation-only-user-reviewed-system-boundary-2026-10-28"
        if preparation_only
        else "analysis-consent-user-reviewed-system-boundary-2026-10-28",
    }


def configured_profile() -> Row | None:
    # Explicit deployment opt-in. No credential file is opened during configuration/preparation.
    model = os.environ.get("PROJECT_LOG_GEMINI_MODEL", "")
    if os.environ.get("PROJECT_LOG_AI_PREPARATION_ONLY") == "1":
        if model not in REVIEWED_MODELS:
            return None
        return public_profile(
            {
                **execution_profile(model, mode="live"),
                "preparation_only": True,
                "free_tier_confirmed": False,
            }
        )
    if (
        os.environ.get("PROJECT_LOG_AI_ENABLED") != "1"
        or os.environ.get("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED") != "1"
        or model not in REVIEWED_MODELS
        or not os.environ.get("PROJECT_LOG_GEMINI_KEY_FILE")
    ):
        return None
    try:
        operator_secret_directory(Path(os.environ["PROJECT_LOG_GEMINI_KEY_FILE"]))
        info = os.lstat(os.environ["PROJECT_LOG_GEMINI_KEY_FILE"])
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not info.st_mode & stat.S_IRUSR
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= 256
        ):
            return None
    except (OSError, ValueError):
        return None
    return public_profile({**execution_profile(model, mode="live"), "free_tier_confirmed": True})


def preparation_available(db: Database, worker: AnalysisWorker | None) -> bool:
    runtime = db.one("SELECT generation,ready FROM analysis_runtime")
    return bool(
        worker
        and worker.profile
        and runtime["ready"]
        and str(runtime["generation"]) == worker.generation
        and (worker.profile["mode"] == "isolated" or configured_profile() == worker.profile)
    )


def execution_available(db: Database, worker: AnalysisWorker | None) -> bool:
    return bool(
        worker
        and worker.profile
        and not worker.profile.get("preparation_only")
        and preparation_available(db, worker)
    )


def request_view(
    db: Database,
    project: str,
    request: str,
    profile: Row | None = None,
    diagnostic_target: Row | None = None,
) -> Row:
    row = db.one(
        "SELECT r.id,r.project_id,r.preparation_id,r.created_at,r.state,r.accepted_at,"
        "r.finished_at,r.started_at,r.cancel_requested_at,r.phase,r.call_seq,r.error_code,"
        "r.result_run_id,r.result_partial,r.resume_from_request_id,r.inherited_attempts,"
        "r.input_digest AS _input_digest,"
        "(SELECT count(*) FROM analysis_events e WHERE e.request_id=r.id) AS event_count,"
        "(SELECT e.data FROM analysis_events e WHERE e.request_id=r.id "
        "AND e.kind='finalization_failed' "
        "ORDER BY event_seq DESC LIMIT 1) AS failure_detail,"
        "(SELECT count(*) FROM analysis_checkpoints c WHERE c.request_id=r.id) AS checkpoint_count,"
        "(SELECT count(*) FROM analysis_checkpoints c WHERE c.request_id=r.id "
        "AND c.origin_request_id<>r.id) AS reused_checkpoints,"
        "(SELECT n.id FROM analysis_requests n WHERE n.resume_from_request_id=r.id "
        "AND n.accepted_at IS NOT NULL) AS resumed_by_request_id,"
        "COALESCE(r.consent->'descriptor',p.descriptor) AS scope "
        "FROM analysis_requests r JOIN analysis_preparations p ON p.id=r.preparation_id "
        "WHERE r.project_id=%s AND r.id=%s",
        (project, request),
    )
    descriptor = row.pop("scope") or {}
    input_digest = row.pop("_input_digest")
    row["execution_budget_policy"] = descriptor.get("profile", {}).get("budget_policy")
    row["resume_block_reason"] = None
    row["resume_after"] = None
    row["diagnostic"] = bool(descriptor.get("diagnostic"))
    row["diagnostic_available"] = False
    row["resume_contract_changed"] = False
    if (
        diagnostic_target
        and str(row["project_id"]) == diagnostic_target["project_id"]
        and str(row["id"]) == diagnostic_target["request_id"]
        and row["state"] == "failed"
        and not row["resumed_by_request_id"]
    ):
        # History/eligibility is metadata-only. Reuse this projection instead of
        # decoding the full immutable input snapshot on every status poll.
        original = {**row, "input_digest": input_digest, "consent": {"descriptor": descriptor}}
        with db.connect() as conn:
            try:
                validate_target(conn, original, diagnostic_target)
                row["diagnostic_available"] = True
            except CollectionError:
                pass
    if (
        profile
        and row["state"] == "failed"
        and row["checkpoint_count"]
        and not row["resumed_by_request_id"]
    ):
        with db.connect() as conn:
            original = {**row, "input_digest": input_digest, "consent": {"descriptor": descriptor}}
            row["resume_block_reason"] = resume_block(conn, original, profile)
            row["resume_contract_changed"] = row[
                "resume_block_reason"
            ] is None and changed_request_resume(original, profile)
        if (
            row["error_code"] == "gemini_free_daily_limit"
            or row["resume_block_reason"] == "analysis_resume_daily_wait"
        ) and row["finished_at"]:
            row["resume_after"] = daily_reset(row["finished_at"]).isoformat()
    row["scope"] = {
        key: descriptor.get(key)
        for key in (
            "end_collection_id",
            "collection_ids",
            "materials",
            "collection_labels",
        )
    }
    return row


def router(db: Database, get_worker: Callable[[], AnalysisWorker | None]) -> APIRouter:
    routes = APIRouter(prefix="/api/projects/{project_id}/analysis")

    def admission() -> AnalysisAdmission:
        worker = get_worker()
        if (
            not worker
            or not worker.profile
            or (worker.profile["mode"] == "live" and configured_profile() != worker.profile)
        ):
            raise CollectionError(
                "analysis_unavailable",
                "분석 실행 설정이 비활성입니다. 운영자 설정과 실행 서버를 확인하세요.",
            )
        return worker.admission

    @routes.get("")
    def status(project_id: UUID) -> Row:
        project = str(project_id)
        worker = get_worker()
        db.one("SELECT id FROM projects WHERE id=%s", (project,))
        available = preparation_available(db, get_worker())
        executable = execution_available(db, get_worker())
        active = db.all(
            "SELECT p.name FROM analysis_requests r JOIN projects p ON p.id=r.project_id "
            "WHERE r.state IN ('queued','running','cancel_requested') LIMIT 1"
        )
        ids = db.all(
            "SELECT id FROM analysis_requests WHERE project_id=%s ORDER BY created_at DESC,id DESC",
            (project,),
        )
        return {
            "available": available,
            "execution_available": executable,
            "message": "로컬 준비 전용입니다. 분석 접수·실행·외부 전송은 차단돼 있습니다."
            if available and not executable
            else None
            if available
            else (
                "분석 실행 설정이 비활성 또는 실행 서버가 준비되지 않았습니다. "
                "저장된 결과는 조회할 수 있습니다."
            ),
            "capacity": {
                "state": "busy" if active else "available" if available else "unknown",
                "projectName": active[0]["name"] if active else None,
            },
            "executions": [
                request_view(
                    db,
                    project,
                    str(r["id"]),
                    worker.profile if worker else None,
                    worker.diagnostic_target if worker else None,
                )
                for r in ids
            ],
        }

    @routes.post("/preparations")
    def prepare(project_id: UUID, body: PrepareAnalysis) -> Row:
        return preparation_view(admission().prepare(str(project_id), body))

    @routes.get("/preparations/{preparation_id}")
    def preparation(project_id: UUID, preparation_id: UUID) -> Row:
        return preparation_view(admission().preparation(str(project_id), str(preparation_id)))

    @routes.post("/requests")
    def submit(project_id: UUID, body: SubmitAnalysis) -> Row:
        admission().submit(str(project_id), body)
        return request_view(db, str(project_id), str(body.request_id))

    @routes.get("/preparations/{preparation_id}/transfer")
    def transfer(
        project_id: UUID,
        preparation_id: UUID,
        response: Response,
        offset: int = Query(default=0, ge=0),
        download: bool = False,
    ) -> Row:
        response.headers["Cache-Control"] = "no-store"
        if download:
            response.headers["Content-Disposition"] = (
                'attachment; filename="analysis-transfer.json"'
            )
        return admission().transfer(
            str(project_id), str(preparation_id), offset=offset, download=download
        )

    @routes.get("/preparations/{preparation_id}/transfer/media/{index}")
    def media(project_id: UUID, preparation_id: UUID, index: int) -> Response:
        mime, raw = admission().transfer_media(str(project_id), str(preparation_id), index)
        return Response(
            raw,
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="analysis-media"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @routes.get("/requests/{request_id}")
    def request(project_id: UUID, request_id: UUID) -> Row:
        worker = get_worker()
        return request_view(
            db,
            str(project_id),
            str(request_id),
            worker.profile if worker else None,
            worker.diagnostic_target if worker else None,
        )

    @routes.get("/requests/{request_id}/events")
    def events(
        project_id: UUID,
        request_id: UUID,
        response: Response,
        http_request: Request,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> Row:
        http_request.state.analysis_read_started = time.monotonic()
        http_request.state.analysis_read_stage = "events_request_lookup"
        project, request = str(project_id), str(request_id)
        original = db.one(
            "SELECT state,consent->'descriptor'->'profile'->>'durability_policy' AS policy "
            "FROM analysis_requests WHERE project_id=%s AND id=%s",
            (project, request),
        )
        response.headers["Cache-Control"] = "no-store"
        excluded = [
            "context",
            "response",
            "schema",
            "candidate",
            "groups",
            "seeds",
            "pending_work",
            "required_units",
            "audit_pairs",
        ]
        http_request.state.analysis_read_stage = "events_page"
        rows = db.all(
            "SELECT event_seq,kind,phase,call_seq_observed,"
            "unit_digest,created_at,data-%s::text[] AS data "
            "FROM analysis_events WHERE project_id=%s AND request_id=%s AND event_seq>%s "
            "ORDER BY event_seq LIMIT %s",
            (excluded, project, request, after, limit),
        )
        http_request.state.analysis_read_stage = "events_count"
        total = db.one(
            "SELECT count(*) AS n FROM analysis_events WHERE project_id=%s AND request_id=%s",
            (project, request),
        )["n"]
        return {
            "events": rows,
            "total": total,
            "after": after,
            "next_after": rows[-1]["event_seq"] if rows else after,
            "execution_state": original["state"],
            "historical_detail_unknown": total == 0
            and original["state"] in {"completed", "failed", "cancelled", "rejected"}
            and original["policy"] != DURABILITY_POLICY,
        }

    @routes.get("/requests/{request_id}/events/{event_seq}")
    def event(
        project_id: UUID,
        request_id: UUID,
        event_seq: int,
        response: Response,
        http_request: Request,
    ) -> Row:
        http_request.state.analysis_read_started = time.monotonic()
        http_request.state.analysis_read_stage = "event_detail"
        response.headers["Cache-Control"] = "no-store"
        return db.one(
            "SELECT event_seq,kind,phase,call_seq_observed,"
            "unit_digest,created_at,data,payload_digest "
            "FROM analysis_events WHERE project_id=%s AND request_id=%s AND event_seq=%s",
            (str(project_id), str(request_id), event_seq),
        )

    @routes.get("/requests/{request_id}/partial")
    def partial(
        project_id: UUID,
        request_id: UUID,
        response: Response,
        http_request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=5, ge=1, le=20),
    ) -> Row:
        from project_log.analysis_events import current_partition
        from project_log.analysis_snapshot import decode_snapshot
        from project_log.experiences import Limits, resolve_chunks, split_evidence

        http_request.state.analysis_read_started = time.monotonic()
        http_request.state.analysis_read_stage = "partial_request_lookup"
        row = db.one(
            "SELECT * FROM analysis_requests WHERE project_id=%s AND id=%s",
            (str(project_id), str(request_id)),
        )
        http_request.state.analysis_read_stage = "partial_read_transaction"
        with db.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            http_request.state.analysis_read_stage = "partial_candidate_validation"
            candidates = validated_candidates(conn, row)
            http_request.state.analysis_read_stage = "partial_partition"
            plan = current_partition(conn, request_id)
            http_request.state.analysis_read_stage = "partial_graph_summary"
            summary = (
                conn.execute(
                    "SELECT data FROM analysis_events WHERE request_id=%s "
                    "AND kind='graph_finished' "
                    "AND event_seq>%s ORDER BY event_seq DESC LIMIT 1",
                    (request_id, plan["event_seq"] if plan else 0),
                ).fetchone()
                if plan
                else None
            )
            http_request.state.analysis_read_stage = "partial_transaction_end"
        chosen = candidates[offset : offset + limit]
        chunks = []
        if chosen:
            snapshot = decode_snapshot(
                row["input_payload"], row["input_manifest"], row["input_digest"]
            )
            refs = {
                c["evidence_id"]
                for candidate in chosen
                for claim in candidate["claims"]
                for c in claim["citations"]
            }
            chunks = [
                {"id": c.id, "references": list(c.references), "offset": c.offset}
                for c in resolve_chunks(
                    split_evidence(
                        snapshot, Limits(**row["consent"]["descriptor"]["profile"]["limits"])
                    ),
                    sorted(refs),
                )
            ]
        response.headers["Cache-Control"] = "no-store"
        return {
            "candidates": chosen,
            "total": len(candidates),
            "offset": offset,
            "chunks": chunks,
            "review_status": "unreviewed",
            "stage_verified": True,
            "complete_analysis": False,
            "unresolved_relations": plan["data"].get("unresolved_relations", []) if plan else [],
            "graph_summary": summary["data"] if summary else None,
            "execution_state": row["state"],
            "historical_detail_unknown": not plan
            and row["state"] in {"completed", "failed", "cancelled", "rejected"}
            and (row.get("consent") or {})
            .get("descriptor", {})
            .get("profile", {})
            .get("durability_policy")
            != DURABILITY_POLICY,
        }

    @routes.get("/requests/{request_id}/partial/evidence")
    def partial_evidence(
        project_id: UUID,
        request_id: UUID,
        reference: str,
        response: Response,
        http_request: Request,
    ) -> Row:
        import json

        from project_log.analysis_snapshot import decode_snapshot

        http_request.state.analysis_read_started = time.monotonic()
        http_request.state.analysis_read_stage = "partial_evidence_request_lookup"
        row = db.one(
            "SELECT input_payload,input_manifest,input_digest FROM analysis_requests "
            "WHERE project_id=%s AND id=%s",
            (str(project_id), str(request_id)),
        )
        snapshot = decode_snapshot(row["input_payload"], row["input_manifest"], row["input_digest"])
        entry = next((e for e in snapshot.entries if e.reference == reference), None)
        if entry is None:
            raise CollectionError("not_found", "해당 고정 입력 근거가 없습니다.")
        response.headers["Cache-Control"] = "no-store"
        return {
            "reference": entry.reference,
            "source": json.loads(entry.source_json),
            "body": entry.text,
            "reason": entry.reason,
            "limitations": list(entry.limitations),
            "media": [m.descriptor() for m in entry.media],
        }

    @routes.post("/requests/{request_id}/cancel")
    def cancel(project_id: UUID, request_id: UUID) -> Row:
        worker = get_worker()
        if not worker:
            raise CollectionError("analysis_unavailable", "분석 실행 서버가 준비되지 않았습니다.")
        worker.state.cancel(str(project_id), str(request_id))
        return request_view(db, str(project_id), str(request_id))

    return routes


def preparation_view(row: Row) -> Row:
    return {
        key: row[key]
        for key in (
            "id",
            "state",
            "end_collection_id",
            "input_digest",
            "consent_digest",
            "descriptor",
            "error_code",
        )
    }
