"""Fenced, short DB transitions; no process waits, Git reads or provider I/O here."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from project_log.analysis_checkpoints import (
    CHECKPOINT_POLICY,
    contract_digest,
    discovery_contract,
    invalid,
    validate_rows,
)
from project_log.analysis_diagnostic import validate_execution
from project_log.analysis_input import canonical
from project_log.analysis_recovery import relation_budget
from project_log.analysis_snapshot import decode_snapshot
from project_log.analysis_transfer import TRANSFER_POLICY
from project_log.db import Database, Row
from project_log.experience_input import EXPERIENCE_INPUT_POLICY, ExperienceInput
from project_log.experience_relations import relation_recovery_policy
from project_log.experience_store import Experiences
from project_log.experiences import (
    ANALYSIS_POLICY,
    BASE_INSTRUCTION,
    CONSOLIDATION_POLICY,
    RESPONSE_SCHEMAS,
    TASK_INSTRUCTIONS,
    AnalysisResult,
    Limits,
)
from project_log.gemini_request import request_policy
from project_log.git import CollectionError
from project_log.policy import sha256
from project_log.worker import AppOwnership

ACTIVE_ANALYSIS = {"queued", "running", "cancel_requested"}
TERMINAL_ANALYSIS = {"completed", "failed", "cancelled", "rejected"}


def execution_profile(model: str, *, mode: str = "isolated") -> Row:
    """Server-owned execution policy; actual external notice is added by the API layer."""
    return {
        "model": model,
        "mode": mode,
        "input_policy": EXPERIENCE_INPUT_POLICY,
        "graph_policy": ANALYSIS_POLICY,
        "transfer_policy": TRANSFER_POLICY,
        "checkpoint_policy": CHECKPOINT_POLICY,
        "consolidation_policy": CONSOLIDATION_POLICY,
        "discovery_contract": discovery_contract(),
        "request_contract": request_policy(),
        "relation_recovery": relation_recovery_policy(),
        "budget_policy": "free-tier-per-execution-checkpoint-progress-v1",
        "minute_retry_max_delay": 60.0,
        "no_progress_resume_limit": 3,
        "instruction_digest": sha256(
            canonical(
                [
                    BASE_INSTRUCTION,
                    TASK_INSTRUCTIONS,
                    {task: schema.model_json_schema() for task, schema in RESPONSE_SCHEMAS.items()},
                ]
            ).encode()
        ),
        "limits": asdict(Limits()),
        "attempts_per_call": 2,
        # One shared cancellable cooldown; still at most two attempts per request.
        "retry_delay": 30.0,
        "retryable": "explicit_http_500_502_503_504_or_bounded_minute_429",
        "result_max_bytes": 32 * 1024 * 1024,
    }


class ExecutionState:
    def __init__(
        self,
        db: Database,
        ownership: AppOwnership,
        generation: str,
        profile: Row | None,
        diagnostic_target: Row | None = None,
    ):
        self.db, self.ownership, self.generation, self.profile = db, ownership, generation, profile
        self.diagnostic_target = diagnostic_target

    def guard(
        self, conn: psycopg.Connection[Row], *, ready: bool = True, require_owner: bool = True
    ) -> None:
        if require_owner:
            self.ownership.check()
        row = conn.execute("SELECT * FROM analysis_runtime FOR SHARE").fetchone()
        if not row or str(row["generation"]) != self.generation or (ready and not row["ready"]):
            raise CollectionError("analysis_owner_lost", "분석 실행 소유권이 종료됐습니다.")

    def fence(self) -> None:
        self.ownership.check()
        with self.db.connect() as conn:
            conn.execute("SELECT * FROM analysis_runtime FOR UPDATE")
            conn.execute(
                "UPDATE analysis_runtime SET generation=%s,ready=false", (self.generation,)
            )

    def closing(self) -> None:
        with self.db.connect() as conn:
            conn.execute("SELECT * FROM analysis_runtime FOR UPDATE")
            conn.execute(
                "UPDATE analysis_runtime SET ready=false WHERE generation=%s", (self.generation,)
            )

    def open(self) -> None:
        with self.db.connect() as conn:
            conn.execute("SELECT * FROM analysis_runtime FOR UPDATE")
            self.ownership.check()
            conn.execute(
                "UPDATE analysis_runtime SET ready=%s WHERE generation=%s",
                (self.profile is not None, self.generation),
            )

    @contextmanager
    def locked(
        self, project: str, request: str, *, ready: bool = True, require_owner: bool = True
    ) -> Iterator[tuple[psycopg.Connection[Row], Row]]:
        with self.db.connect() as conn:
            self.guard(conn, ready=ready, require_owner=require_owner)
            conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,))
            row = conn.execute(
                "SELECT * FROM analysis_requests WHERE project_id=%s AND id=%s FOR UPDATE",
                (project, request),
            ).fetchone()
            if row is None:
                raise CollectionError("not_found", "해당 분석 실행이 없습니다.")
            yield conn, row

    def validate(self, row: Row) -> None:
        consent = row["consent"]
        if (
            str(row["generation"]) != self.generation
            or row["accepted_at"] is None
            or not consent
            or consent.get("confirmed") is not True
            or consent.get("input_digest") != row["input_digest"]
            or self.profile is None
            or consent["descriptor"]["profile"] != self.profile
            or consent["descriptor"].get("transfer_policy") != TRANSFER_POLICY
            or row["input_manifest"].get("policy") != EXPERIENCE_INPUT_POLICY
        ):
            raise CollectionError(
                "analysis_consent_mismatch", "접수된 동의·실행 정책이 일치하지 않습니다."
            )
        validate_execution(row, self.diagnostic_target)

    def owned(self, row: Row, owner: str) -> None:
        self.validate(row)
        if str(row["owner_token"]) != owner:
            raise CollectionError("analysis_owner_lost", "분석 실행 소유권이 종료됐습니다.")

    def cancel(self, project: str, request: str) -> Row:
        with self.locked(project, request) as (conn, row):
            if row["state"] in {"queued", "running"}:
                conn.execute(
                    "UPDATE analysis_requests SET state='cancel_requested',"
                    "cancel_requested_at=clock_timestamp() WHERE id=%s",
                    (request,),
                )
        return self.db.one(
            "SELECT id,state,cancel_requested_at FROM analysis_requests WHERE id=%s", (request,)
        )

    def claim(self) -> Row | None:
        if self.profile and self.profile.get("preparation_only"):
            return None
        rows = self.db.all(
            "SELECT id,project_id FROM analysis_requests WHERE state='queued' "
            "ORDER BY created_at,id LIMIT 1"
        )
        if not rows:
            return None
        project, request = str(rows[0]["project_id"]), str(rows[0]["id"])
        with self.locked(project, request) as (conn, row):
            if row["state"] != "queued":
                return None
            self.validate(row)
            return conn.execute(
                "UPDATE analysis_requests SET state='running',owner_token=%s,"
                "started_at=clock_timestamp(),phase='starting' WHERE id=%s RETURNING *",
                (uuid4(), request),
            ).fetchone()

    def load(self, project: str, request: str, owner: str) -> ExperienceInput:
        row = self.db.one(
            "SELECT * FROM analysis_requests WHERE project_id=%s AND id=%s", (project, request)
        )
        self.owned(row, owner)
        return decode_snapshot(row["input_payload"], row["input_manifest"], row["input_digest"])

    def child_ready(self, project: str, request: str, owner: str, identity: list[int]) -> None:
        with self.locked(project, request) as (conn, row):
            self.owned(row, owner)
            if row["state"] != "running" or row["child_identity"] is not None:
                raise CollectionError("analysis_stopped", "실행 시작 승인이 종료됐습니다.")
            conn.execute(
                "UPDATE analysis_requests SET child_identity=%s,phase='ready' WHERE id=%s",
                (Jsonb(identity), request),
            )

    def enter_call(
        self,
        project: str,
        request: str,
        owner: str,
        seq: int,
        phase: str,
        digest: str,
        variant: str | None = None,
        feedback_code: str | None = None,
        logical_digest: str | None = None,
    ) -> None:
        if self.profile and self.profile.get("preparation_only"):
            raise CollectionError("analysis_transfer_disabled", "로컬 준비 전용입니다.")
        with self.locked(project, request) as (conn, row):
            self.owned(row, owner)
            if (
                row["state"] != "running"
                or row["child_identity"] is None
                or seq != row["call_seq"] + 1
            ):
                raise CollectionError("analysis_stopped", "새 모델 호출은 허용되지 않습니다.")
            assert self.profile is not None
            diagnostic = validate_execution(row, self.diagnostic_target)
            identity = None
            if phase == "reconcile" and not diagnostic and self.profile.get("relation_recovery"):
                if logical_digest is None:
                    raise CollectionError(
                        "analysis_control_invalid", "관계 요청 식별자가 없습니다."
                    )
                budget = relation_budget(conn, row, self.profile, logical_digest)
                identity = budget["logical_digest"]
                expected = "feedback" if budget["feedback_code"] else "initial"
                if (
                    not budget["applicable"]
                    or variant != expected
                    or feedback_code != budget["feedback_code"]
                ):
                    raise CollectionError(
                        "analysis_relation_feedback_invalid", "승인된 복구 variant가 아닙니다."
                    )
                if budget["wire_digest"] and digest != budget["wire_digest"]:
                    raise CollectionError(
                        "analysis_input_mismatch", "동일 요청의 재시도 입력이 변경됐습니다."
                    )
            if diagnostic and (
                seq != 1 or phase != "reconcile" or digest != diagnostic["wire_digest"]
            ):
                raise CollectionError(
                    "analysis_diagnostic_wire_mismatch",
                    "허용된 기존 실패 요청의 동일 payload 1회만 전송할 수 있습니다.",
                )
            if seq > self.profile["limits"]["max_calls"]:
                raise CollectionError(
                    "analysis_budget_exceeded", "실제 전송 attempt 예산을 초과했습니다."
                )
            conn.execute(
                "INSERT INTO analysis_calls"
                "(request_id,project_id,generation,owner_token,seq,phase,"
                "payload_digest,logical_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (request, project, self.generation, owner, seq, phase, digest, identity),
            )
            conn.execute(
                "UPDATE analysis_requests SET call_seq=%s,phase=%s WHERE id=%s",
                (seq, phase, request),
            )

    def checkpoint(self, project: str, request: str, owner: str, payload: Row) -> None:
        """Commit one complete validated response before granting further Graph work."""
        with self.locked(project, request) as (conn, row):
            self.owned(row, owner)
            if row["consent"]["descriptor"].get("diagnostic"):
                raise CollectionError(
                    "analysis_diagnostic_stopped", "진단 응답은 중간 결과로 저장하지 않습니다."
                )
            if row["state"] != "running":
                raise CollectionError("analysis_stopped", "중간 결과 저장 승인이 종료됐습니다.")
            assert self.profile is not None
            ordinal, task = payload["ordinal"], payload["task"]
            if type(ordinal) is not int or not isinstance(task, str):
                raise invalid()
            previous = conn.execute(
                "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal",
                (request,),
            ).fetchall()
            item = {
                "ordinal": ordinal,
                "task": task,
                "contract_digest": contract_digest(row["input_digest"], self.profile),
                "request_digest": payload["request_digest"],
                "response": payload["response"],
                "response_digest": sha256(canonical(payload["response"]).encode()),
            }
            if ordinal <= len(previous):
                if ordinal < 1 or any(previous[ordinal - 1][k] != v for k, v in item.items()):
                    raise invalid()
                return  # Resolve a repeated durable save without overwriting anything.
            validate_rows(previous + [item], row["input_digest"], self.profile)
            attempt = conn.execute(
                "SELECT * FROM analysis_calls WHERE request_id=%s AND seq=%s",
                (request, row["call_seq"]),
            ).fetchone()
            if (
                not attempt
                or attempt["phase"] != task
                or attempt["outcome"] != "responded"
                or attempt["error_code"] is not None
                or str(attempt["owner_token"]) != owner
                or any(
                    str(p["origin_request_id"]) == request
                    and p["origin_call_seq"] >= row["call_seq"]
                    for p in previous
                )
            ):
                raise invalid()
            conn.execute(
                "INSERT INTO analysis_checkpoints(request_id,project_id,ordinal,task,"
                "contract_digest,request_digest,response,response_digest,"
                "origin_request_id,origin_call_seq) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request,
                    project,
                    ordinal,
                    task,
                    item["contract_digest"],
                    item["request_digest"],
                    Jsonb(item["response"]),
                    item["response_digest"],
                    request,
                    row["call_seq"],
                ),
            )

    def relation_state(self, project: str, request: str, owner: str, logical_digest: str) -> Row:
        with self.locked(project, request) as (conn, row):
            self.owned(row, owner)
            if row["state"] != "running" or self.profile is None:
                raise CollectionError("analysis_stopped", "관계 실행이 중단됐습니다.")
            budget = relation_budget(conn, row, self.profile, logical_digest)
            if not budget["applicable"]:
                raise CollectionError(
                    "analysis_relation_budget_unknown", "미완료 관계 작업을 확인할 수 없습니다."
                )
            return budget

    def relation_failed(self, project: str, request: str, owner: str, seq: int, code: str) -> None:
        from project_log.experience_relations import grounding_feedback

        if type(seq) is not int or not isinstance(code, str) or grounding_feedback(code) is None:
            raise CollectionError(
                "analysis_relation_feedback_invalid", "분류되지 않은 복구 조건입니다."
            )
        with self.locked(project, request, ready=False) as (conn, row):
            self.owned(row, owner)
            if row["call_seq"] != seq:
                raise CollectionError("analysis_control_invalid", "관계 응답 순서가 다릅니다.")
            updated = conn.execute(
                "UPDATE analysis_calls SET error_code=%s WHERE request_id=%s AND owner_token=%s "
                "AND seq=%s AND phase='reconcile' AND outcome='responded' "
                "AND (error_code IS NULL OR error_code=%s) RETURNING seq",
                (code, request, owner, seq, code),
            ).fetchone()
            if updated is None:
                raise CollectionError("analysis_control_invalid", "관계 응답을 확정할 수 없습니다.")

    def phase(self, project: str, request: str, owner: str, phase: str) -> None:
        with self.locked(project, request) as (conn, row):
            self.owned(row, owner)
            if row["state"] == "running":
                conn.execute("UPDATE analysis_requests SET phase=%s WHERE id=%s", (phase, request))

    def call_finished(
        self, project: str, request: str, owner: str, seq: int, code: str | None
    ) -> None:
        with self.locked(project, request, ready=False) as (conn, row):
            self.owned(row, owner)
            conn.execute(
                "UPDATE analysis_calls SET outcome=%s,error_code=%s,finished_at=clock_timestamp() "
                "WHERE request_id=%s AND owner_token=%s AND seq=%s AND outcome='entered'",
                ("failed" if code else "responded", code, request, owner, seq),
            )

    def finish(
        self,
        project: str,
        request: str,
        owner: str | None,
        *,
        code: str | None = None,
        snapshot: ExperienceInput | None = None,
        result: AnalysisResult | None = None,
    ) -> None:
        # Supervisor calls only after join/quiescence. Cancellation is authoritative.
        with self.locked(
            project, request, ready=result is not None, require_owner=result is not None
        ) as (conn, row):
            if row["state"] in TERMINAL_ANALYSIS:
                return
            if str(row["owner_token"]) != str(owner):
                raise CollectionError("analysis_owner_lost", "분석 실행 소유권이 종료됐습니다.")
            state = (
                "cancelled"
                if row["state"] == "cancel_requested" and code in {None, "analysis_stopped"}
                else "failed"
            )
            run = None
            if result is not None and row["state"] == "running":
                if row["consent"]["descriptor"].get("diagnostic"):
                    raise CollectionError(
                        "analysis_diagnostic_stopped", "진단은 최종 분석 결과로 저장하지 않습니다."
                    )
                assert owner is not None and snapshot is not None and self.profile is not None
                self.owned(row, owner)
                if (
                    snapshot.digest != row["input_digest"]
                    or result.policy != self.profile["graph_policy"]
                ):
                    raise CollectionError(
                        "analysis_input_mismatch", "결과와 고정 입력이 일치하지 않습니다."
                    )
                run = Experiences(self.db).insert(
                    conn, snapshot, result, model=self.profile["model"], mode=self.profile["mode"]
                )
                state = "completed"
            conn.execute(
                "UPDATE analysis_requests SET state=%s,error_code=%s,result_run_id=%s,"
                "finished_at=clock_timestamp(),phase=%s WHERE id=%s",
                (state, code, run, state, request),
            )
            self.unresolved_calls(conn, request)

    @staticmethod
    def unresolved_calls(conn: psycopg.Connection[Row], request: str) -> None:
        # Only after child quiescence/fencing. A local grant is not proof of
        # delivery or a provider result. Never invent a response or refund it.
        conn.execute(
            "UPDATE analysis_calls SET outcome='failed',error_code='analysis_call_outcome_unknown',"
            "finished_at=clock_timestamp() WHERE request_id=%s AND outcome='entered'",
            (request,),
        )

    def recover(self, *, code: str, require_owner: bool = True) -> None:
        # Called after fence AND old local execution quiescence, never as a timeout shortcut.
        for row in self.db.all(
            "SELECT project_id,id FROM analysis_requests "
            "WHERE state IN ('queued','running','cancel_requested','checking')"
        ):
            with self.locked(
                str(row["project_id"]), str(row["id"]), ready=False, require_owner=require_owner
            ) as (conn, current):
                if current["state"] not in ACTIVE_ANALYSIS | {"checking"}:
                    continue
                conn.execute(
                    "UPDATE analysis_requests SET state=%s,error_code=%s,phase='interrupted',"
                    "finished_at=clock_timestamp() WHERE id=%s",
                    ("rejected" if current["accepted_at"] is None else "failed", code, row["id"]),
                )
                self.unresolved_calls(conn, str(row["id"]))
        with self.db.connect() as conn:
            self.guard(conn, ready=False, require_owner=require_owner)
            conn.execute(
                "UPDATE analysis_preparations SET state='failed',error_code=%s "
                "WHERE state='preparing'",
                (code,),
            )
