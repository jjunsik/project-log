"""Local preparation and atomic admission. Runtime ownership/dispatch are separate.

The app supervisor must open analysis_runtime under its single-process ownership
before using this service. This module never reads a credential or calls a model.
"""

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from project_log.analysis_checkpoints import (
    DISCOVERY_COMPATIBILITY,
    PLANNING_COMPATIBILITY,
    RELATION_FIX_COMPATIBILITY,
    RELATION_RECOVERY_COMPATIBILITY,
    REQUEST_COMPATIBILITY,
    compatibility_policy,
    contract_digest,
    resume_rows,
)
from project_log.analysis_diagnostic import (
    capture_request,
    normalize,
    validate_target,
    wire_document,
)
from project_log.analysis_input import canonical
from project_log.analysis_recovery import changed_request_resume, resume_block
from project_log.analysis_snapshot import decode_snapshot, encode_snapshot
from project_log.analysis_sources import source_version
from project_log.analysis_transfer import TRANSFER_POLICY
from project_log.db import Database, Row
from project_log.experience_input import ExperienceInput, ExperienceInputs
from project_log.experience_planning import PLANNING_POLICY
from project_log.experience_relations import (
    CORE_SEEDS,
    EXCERPT_CHARS,
    RELATION_CONTEXT_CHARS,
    RELATION_POLICY,
    WINDOW_SEEDS,
)
from project_log.experiences import (
    BASE_INSTRUCTION,
    CONSOLIDATION_MAX_SEEDS,
    CONSOLIDATION_POLICY,
    RESPONSE_SCHEMAS,
    TASK_INSTRUCTIONS,
    Limits,
    discovery_schema,
    plan_discovery,
    split_evidence,
)
from project_log.gemini_request import provider_schema, request_policy
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.policy import sha256

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
REQUEST_FIELDS = (
    "id,project_id,preparation_id,generation,created_at,state,accepted_at,finished_at,"
    "input_digest,error_code,result_run_id,result_partial,started_at,cancel_requested_at,phase,call_seq,"
    "resume_from_request_id,inherited_attempts"
)


class PrepareAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparation_id: UUID
    end_collection_id: UUID
    resume_from_request_id: UUID | None = None
    diagnostic_retry: StrictBool = False


class SubmitAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    preparation_id: UUID
    input_digest: Digest
    consent_digest: Digest
    relation_recovery_digest: Digest | None = None
    relation_transition_digest: Digest | None = None
    confirmed: StrictBool


@dataclass(frozen=True)
class CandidateInput:
    manifest: Row
    digest: str
    payload_json: str
    expires: float
    diagnostic_wire: bytes | None = None
    relation_transition: Row | None = None


class AnalysisAdmission:
    def __init__(
        self,
        db: Database,
        materials: Materials,
        generation: str,
        profile: Row,
        diagnostic_target: Row | None = None,
    ):
        self.db, self.materials, self.generation = db, materials, generation
        # Defensive copy: a mutable caller profile must not silently change consent.
        self.profile: Row = json.loads(canonical(profile))
        self.diagnostic_target = normalize(diagnostic_target) if diagnostic_target else None
        self.cache: dict[str, CandidateInput] = {}
        self.cache_lock = threading.Lock()

    def _guard(self, conn: psycopg.Connection[Row]) -> None:
        row = conn.execute("SELECT * FROM analysis_runtime FOR SHARE").fetchone()
        if not row or not row["ready"] or str(row["generation"]) != self.generation:
            raise CollectionError("analysis_unavailable", "분석 실행 서버가 준비되지 않았습니다.")

    def _version(self, project: str, end: str) -> Row:
        with self.db.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return source_version(conn, project, end)

    def _cached(self, preparation: str) -> CandidateInput | None:
        with self.cache_lock:
            now = time.monotonic()
            for key in list(self.cache):
                if self.cache[key].expires <= now:
                    del self.cache[key]
            return self.cache.get(preparation)

    def _keep(self, preparation: str, candidate: CandidateInput) -> None:
        self._cached(preparation)
        with self.cache_lock:
            # Bounded temporary memory, not a durable draft store or input truncation.
            if (
                len(self.cache) >= 8
                or sum(
                    len(c.payload_json.encode()) + len(c.diagnostic_wire or b"")
                    for c in self.cache.values()
                )
                + len(candidate.payload_json.encode())
                + len(candidate.diagnostic_wire or b"")
                > 128 * 1024 * 1024
            ):
                raise CollectionError(
                    "analysis_preparation_capacity",
                    "임시 입력 공간이 부족합니다. 잠시 후 다시 준비하세요.",
                )
            self.cache[preparation] = candidate

    def preparation(self, project: str, preparation: str) -> Row:
        row = self.db.one(
            "SELECT * FROM analysis_preparations WHERE project_id=%s AND id=%s",
            (project, preparation),
        )
        if (
            row["state"] in {"preparing", "ready", "empty"}
            and str(row["generation"]) != self.generation
        ):
            return {**row, "state": "stale", "error_code": "analysis_preparation_lost"}
        if row["state"] in {"ready", "empty"}:
            candidate = self._cached(preparation)
            if candidate is None:
                return {**row, "state": "stale", "error_code": "analysis_preparation_lost"}
            try:
                current = self._version(project, str(row["end_collection_id"]))
                changed = current["fingerprint"] != row["source_fingerprint"]
            except CollectionError:
                changed = True
            if (
                changed
                or row["descriptor"]["profile"] != self.profile
                or row["descriptor"].get("transfer_policy") != TRANSFER_POLICY
            ):
                return {**row, "state": "stale", "error_code": "analysis_input_changed"}
        return row

    def prepare(self, project: str, body: PrepareAnalysis) -> Row:
        preparation, end = str(body.preparation_id), str(body.end_collection_id)
        resume = str(body.resume_from_request_id) if body.resume_from_request_id else None
        if body.diagnostic_retry and not resume:
            raise CollectionError(
                "analysis_diagnostic_unavailable", "진단할 실패 실행이 필요합니다."
            )
        with self.db.connect() as conn:
            self._guard(conn)
            if not conn.execute(
                "SELECT id FROM projects WHERE id=%s FOR SHARE", (project,)
            ).fetchone():
                raise CollectionError("not_found", "해당 프로젝트가 없습니다.")
            inserted = conn.execute(
                "INSERT INTO analysis_preparations"
                "(id,project_id,end_collection_id,generation,state,"
                "resume_from_request_id,descriptor) "
                "VALUES (%s,%s,%s,%s,'preparing',%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                (
                    preparation,
                    project,
                    end,
                    self.generation,
                    resume,
                    Jsonb({"diagnostic_retry": True}) if body.diagnostic_retry else None,
                ),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM analysis_preparations WHERE id=%s", (preparation,)
            ).fetchone()
            if (
                not row
                or str(row["project_id"]) != project
                or str(row["end_collection_id"]) != end
                or str(row["resume_from_request_id"]) != str(resume)
                or bool((row["descriptor"] or {}).get("diagnostic_retry")) != body.diagnostic_retry
            ):
                raise CollectionError("conflict", "같은 준비 ID를 다른 범위에 사용할 수 없습니다.")
        if not inserted:
            return self.preparation(project, preparation)
        try:
            before = self._version(project, end)
            if resume:
                with self.db.connect() as conn:
                    original, original_preparation, checkpoints = self._resume_source(
                        conn, project, resume, end, diagnostic=body.diagnostic_retry
                    )
                if before["fingerprint"] != original_preparation["source_fingerprint"]:
                    raise CollectionError(
                        "analysis_input_changed", "원천 자료가 변경돼 재개할 수 없습니다."
                    )
                # No Git, repository files, material reads, extraction or new prepared_at.
                snapshot = decode_snapshot(
                    original["input_payload"], original["input_manifest"], original["input_digest"]
                )
            else:
                snapshot = ExperienceInputs(self.db, self.materials).prepare(
                    project, end_collection=end
                )
            after = self._version(project, end)
            if (
                before["fingerprint"] != after["fingerprint"]
                or canonical(before["collections"]) != snapshot.collections_json
            ):
                raise CollectionError(
                    "analysis_input_changed", "자료가 변경됐습니다. 다시 준비하고 동의하세요."
                )
            manifest, digest = snapshot.manifest(), snapshot.digest
            descriptor = self._descriptor(snapshot, before, end)
            if resume:
                descriptor["resume"] = self._resume_descriptor(original, checkpoints)
            transition = descriptor.get("resume", {}).get("transition")
            candidate = CandidateInput(
                manifest,
                digest,
                canonical(encode_snapshot(snapshot)),
                time.monotonic() + 1800,
                capture_request(snapshot, checkpoints, self.profile, self.diagnostic_target)
                if body.diagnostic_retry and self.diagnostic_target
                else None,
                transition,
            )
            if body.diagnostic_retry:
                descriptor["diagnostic_retry"] = True
                descriptor["diagnostic"] = self.diagnostic_target
            if body.diagnostic_retry or transition:
                descriptor["transfer_digest"] = self._candidate_document(snapshot, candidate)[
                    "transfer_digest"
                ]
            consent_digest = self._consent(
                project, preparation, digest, before["fingerprint"], descriptor
            )
            self._keep(preparation, candidate)
            with self.db.connect() as conn:
                self._guard(conn)
                conn.execute(
                    "UPDATE analysis_preparations SET state=%s,source_fingerprint=%s,"
                    "input_digest=%s,consent_digest=%s,descriptor=%s "
                    "WHERE id=%s AND state='preparing'",
                    (
                        "ready" if descriptor["processable_count"] else "empty",
                        before["fingerprint"],
                        digest,
                        consent_digest,
                        Jsonb(descriptor),
                        preparation,
                    ),
                )
        except Exception as exc:
            with self.cache_lock:
                self.cache.pop(preparation, None)
            self.db.execute(
                "UPDATE analysis_preparations SET state='failed',error_code=%s "
                "WHERE id=%s AND state='preparing'",
                (
                    exc.code if isinstance(exc, CollectionError) else "analysis_preparation_failed",
                    preparation,
                ),
            )
            if not isinstance(exc, CollectionError):
                raise
        return self.preparation(project, preparation)

    def _resume_source(
        self,
        conn: psycopg.Connection[Row],
        project: str,
        request: str,
        end: str,
        *,
        diagnostic: bool = False,
    ) -> tuple[Row, Row, list[Row]]:
        original = conn.execute(
            "SELECT * FROM analysis_requests WHERE project_id=%s AND id=%s", (project, request)
        ).fetchone()
        if not original or original["state"] != "failed" or original["accepted_at"] is None:
            raise CollectionError(
                "analysis_resume_unavailable", "실패 확정된 실행만 재개할 수 있습니다."
            )
        if conn.execute(
            "SELECT id FROM analysis_requests WHERE resume_from_request_id=%s "
            "AND accepted_at IS NOT NULL",
            (request,),
        ).fetchone():
            raise CollectionError(
                "analysis_resume_consumed", "이미 접수된 재개 실행의 상태를 확인하세요."
            )
        prepared = conn.execute(
            "SELECT * FROM analysis_preparations WHERE id=%s", (original["preparation_id"],)
        ).fetchone()
        assert prepared is not None
        if (
            str(prepared["end_collection_id"]) != end
            or original["consent"]["descriptor"].get("transfer_policy") != TRANSFER_POLICY
        ):
            raise CollectionError(
                "analysis_policy_changed", "재개 계약이 달라졌습니다. 새 분석을 준비하세요."
            )
        checkpoints = conn.execute(
            "SELECT * FROM analysis_checkpoints WHERE project_id=%s AND request_id=%s "
            "ORDER BY ordinal",
            (project, request),
        ).fetchall()
        if not checkpoints:
            raise CollectionError(
                "analysis_resume_unavailable", "재사용할 완료 중간 결과가 없습니다."
            )
        if original["consent"]["descriptor"].get("diagnostic") and (
            diagnostic or not changed_request_resume(original, self.profile)
        ):
            raise CollectionError(
                "analysis_diagnostic_consumed", "진단 실행의 두 번째 진단은 허용하지 않습니다."
            )
        if diagnostic:
            validate_target(conn, original, self.diagnostic_target)
        blocked = None if diagnostic else resume_block(conn, original, self.profile)
        if blocked:
            raise CollectionError(
                blocked, "재개 조건을 확인하세요. 완료 중간 결과와 원본 이력은 보존됩니다."
            )
        snapshot = decode_snapshot(
            original["input_payload"], original["input_manifest"], original["input_digest"]
        )
        # Provenance is independent of JSON hashes: a response must originate in
        # this Project's completed, clean provider call and retain its chronology.
        for checkpoint in checkpoints:
            origin = conn.execute(
                "SELECT * FROM analysis_calls WHERE request_id=%s AND seq=%s AND project_id=%s",
                (checkpoint["origin_request_id"], checkpoint["origin_call_seq"], project),
            ).fetchone()
            if (
                not origin
                or origin["phase"] != checkpoint["task"]
                or origin["outcome"] != "responded"
                or origin["error_code"] is not None
                or (
                    origin["unit_request_digest"] is not None
                    and origin["unit_request_digest"] != checkpoint["request_digest"]
                )
            ):
                raise CollectionError(
                    "analysis_checkpoint_invalid", "중간 결과 출처가 유효하지 않습니다."
                )
            checkpoint["_origin_unit_digest"] = origin["unit_request_digest"]
        old_profile = original["consent"]["descriptor"]["profile"]
        proof_events = None
        if compatibility_policy(old_profile, self.profile) == PLANNING_COMPATIBILITY:
            proof_events = conn.execute(
                "SELECT event_seq,kind,phase,unit_digest,data FROM analysis_events "
                "WHERE request_id=%s AND kind IN ('unit_started','unit_reused','unit_validated') "
                "ORDER BY event_seq",
                (request,),
            ).fetchall()
        checkpoints = resume_rows(
            checkpoints, snapshot, old_profile, self.profile, proof_events=proof_events
        )
        return original, prepared, checkpoints

    def _resume_descriptor(self, original: Row, checkpoints: list[Row]) -> Row:
        result: Row = {
            "request_id": str(original["id"]),
            "checkpoint_count": len(checkpoints),
            "inherited_attempts": original["inherited_attempts"] + original["call_seq"],
        }
        old = original["consent"]["descriptor"]["profile"]
        if old != self.profile:
            result["compatibility"] = {
                "policy": compatibility_policy(old, self.profile),
                "reused_stage": "discover"
                if compatibility_policy(old, self.profile) == DISCOVERY_COMPATIBILITY
                else (
                    "discover_consolidate"
                    if compatibility_policy(old, self.profile)
                    in (
                        REQUEST_COMPATIBILITY,
                        RELATION_RECOVERY_COMPATIBILITY,
                        RELATION_FIX_COMPATIBILITY,
                    )
                    else "all_completed"
                ),
                "source_contract": contract_digest(original["input_digest"], old),
                "target_contract": contract_digest(original["input_digest"], self.profile),
            }
            if result["compatibility"]["policy"] == RELATION_FIX_COMPATIBILITY:
                result["transition"] = {
                    "policy": RELATION_FIX_COMPATIBILITY,
                    "source_contract": result["compatibility"]["source_contract"],
                    "target_contract": result["compatibility"]["target_contract"],
                    "previous_attempts": 2,
                    "max_attempts": 2,
                    "max_regenerations": 1,
                    "scope": "one_target_execution_lineage_per_fixed_sources_then_normal_resume",
                    "old_response_or_feedback_reused": False,
                }
                result["transition"]["digest"] = sha256(canonical(result).encode())
        return result

    def _descriptor(self, snapshot: ExperienceInput, before: Row, end: str) -> Row:
        project = snapshot.project_id
        descriptor: Row = {
            "end_collection_id": end,
            "collection_ids": [str(c["id"]) for c in before["collections"]],
            "materials": before["materials"],
            "collection_labels": self.db.all(
                "SELECT id,title,created_at FROM collections WHERE project_id=%s "
                "AND id=ANY(%s) ORDER BY created_at,id",
                (project, [c["id"] for c in before["collections"]]),
            ),
            "manifest": snapshot.manifest(),
            "transfer_policy": TRANSFER_POLICY,
            "transfer_digest": self._transfer_document(snapshot)["transfer_digest"],
            "items": [
                {
                    "reference": e.reference,
                    "reason": e.reason,
                    "limitations": list(e.limitations),
                    "outcome": "excluded"
                    if e.reason or not (e.text or any(m.use_for_analysis for m in e.media))
                    else "partial"
                    if e.limitations
                    else "ready",
                }
                for e in snapshot.entries
            ],
            "profile": self.profile,
            "relation_recovery_digest": self._recovery_digest(),
            "collection_scope": "최초 완료 수집부터 선택한 종료 수집까지",
            "material_scope": "정상 접수 당시 해당 프로젝트의 추가 자료 전체",
            "processable_count": sum(
                not e.reason and (bool(e.text) or any(m.use_for_analysis for m in e.media))
                for e in snapshot.entries
            ),
        }
        result: Row = json.loads(canonical(descriptor))
        return result

    def _recovery_digest(self) -> str | None:
        policy = self.profile.get("relation_recovery")
        return sha256(canonical(policy).encode()) if policy else None

    def _transfer_document(self, snapshot: ExperienceInput, *, include_bytes: bool = False) -> Row:
        chunks = split_evidence(snapshot, Limits(**self.profile["limits"]))
        document: Row = {
            "policy": TRANSFER_POLICY,
            "input_digest": snapshot.digest,
            "system_instruction": BASE_INSTRUCTION,
            "request_contract": request_policy(),
            "relation_recovery": self.profile.get("relation_recovery"),
            "relation_recovery_digest": self._recovery_digest(),
            **(
                {"planning_policy": self.profile["planning_policy"]}
                if self.profile.get("planning_policy")
                else {}
            ),
            **(
                {"durability_policy": self.profile["durability_policy"]}
                if self.profile.get("durability_policy")
                else {}
            ),
            **(
                {"failure_policy": self.profile["failure_policy"]}
                if self.profile.get("failure_policy")
                else {}
            ),
            **(
                {
                    "discovery_plan": [
                        {"origin_ordinal": origin, "chunk_ids": [c.id for c in part]}
                        for part, origin in zip(
                            *plan_discovery(chunks, Limits(**self.profile["limits"]), True),
                            strict=True,
                        )
                    ]
                }
                if self.profile.get("planning_policy") == PLANNING_POLICY
                else {}
            ),
            "task_instructions": TASK_INSTRUCTIONS,
            "response_schemas": {
                task: (
                    [
                        {
                            "batch_index": index,
                            "schema": discovery_schema(batch).model_json_schema(),
                            "provider_schema": provider_schema(
                                discovery_schema(batch).model_json_schema()
                            ),
                        }
                        for index, batch in enumerate(
                            plan_discovery(
                                chunks,
                                Limits(**self.profile["limits"]),
                                self.profile.get("planning_policy") == PLANNING_POLICY,
                            )[0]
                        )
                    ]
                    if task == "discover"
                    else schema.model_json_schema()
                )
                for task, schema in RESPONSE_SCHEMAS.items()
            },
            "provider_response_schemas": {
                task: provider_schema(schema.model_json_schema())
                for task, schema in RESPONSE_SCHEMAS.items()
                if task != "discover"
            },
            "generation": {
                "candidateCount": 1,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
            "chunks": [chunk.payload() for chunk in chunks],
            "later_context": (
                "후속 호출 문맥은 이 고정 자료와 이번 실행의 AI 응답으로 구성됩니다. "
                "관계 feedback에는 동일 작업의 이전 실행에서 보존한 비민감 검증 code/count도 "
                "포함될 수 있습니다. 실패 응답 원문은 재전송하지 않습니다."
                if self.profile.get("relation_recovery")
                else "후속 호출 문맥은 이 고정 자료와 이번 실행의 AI 응답으로 구성됩니다."
            ),
            "consolidation": {
                "policy": CONSOLIDATION_POLICY,
                "max_seeds_per_batch": CONSOLIDATION_MAX_SEEDS,
                "batch_chars": self.profile["limits"]["batch_chars"],
                "seed_id_format": "seed_{batch_position:04d}",
                "relation_policy": RELATION_POLICY,
                "global_seed_id_format": "seed_{global_position:06d}",
                "review_core_seeds": CORE_SEEDS,
                "review_window_seeds": WINDOW_SEEDS,
                "support_excerpt_chars": EXCERPT_CHARS,
                "review_context_chars": RELATION_CONTEXT_CHARS,
                "review_scope": (
                    "모든 Seed를 전역 인덱스에 조회하고 배치와 무관한 제한 문맥에서 재분할합니다. "
                    "검색 누락/모델 판단의 정확성을 보장하지 않습니다. "
                    "초기 그룹은 잠정이며 모순 관계는 실패합니다."
                ),
                "assignment_schema": (
                    "발견 후 각 배치의 실제 Seed ID 전부를 필수 키로 지정하며 "
                    "그룹 label은 0~배치 크기-1입니다."
                ),
            },
        }
        document["transfer_digest"] = sha256(canonical(document).encode())
        if include_bytes:
            document["inline_media"] = {
                str(i): {
                    "mime_type": c.media.mime_type,
                    "base64": base64.b64encode(c.media.raw).decode("ascii"),
                }
                for i, c in enumerate(chunks)
                if c.media is not None
            }
        return document

    def _preview_snapshot(self, project: str, preparation: str) -> tuple[Row, ExperienceInput]:
        row = self.preparation(project, preparation)
        candidate = self._cached(preparation)
        if row["state"] not in {"ready", "empty"} or candidate is None:
            raise CollectionError(
                "analysis_preparation_lost", "다시 준비한 뒤 전송 내용을 확인하세요."
            )
        snapshot = decode_snapshot(
            json.loads(candidate.payload_json), candidate.manifest, candidate.digest
        )
        return row, snapshot

    def _candidate_document(
        self, snapshot: ExperienceInput, candidate: CandidateInput, *, include_bytes: bool = False
    ) -> Row:
        document = self._transfer_document(snapshot, include_bytes=include_bytes)
        if candidate.relation_transition is not None:
            document["relation_transition"] = candidate.relation_transition
        if candidate.diagnostic_wire is not None:
            if not self.diagnostic_target:
                raise CollectionError("analysis_diagnostic_unavailable", "진단 설정을 확인하세요.")
            document["diagnostic_request"] = wire_document(
                candidate.diagnostic_wire, self.diagnostic_target
            )
            # Bind the exact outgoing request, including prior AI-derived context,
            # as well as the original fixed materials. Inline bytes remain hash-bound.
        if candidate.diagnostic_wire is not None or candidate.relation_transition is not None:
            document.pop("transfer_digest")
            without_bytes = {k: v for k, v in document.items() if k != "inline_media"}
            document["transfer_digest"] = sha256(canonical(without_bytes).encode())
        return document

    def transfer(
        self, project: str, preparation: str, *, offset: int = 0, download: bool = False
    ) -> Row:
        row, snapshot = self._preview_snapshot(project, preparation)
        candidate = self._cached(preparation)
        assert candidate is not None
        document = self._candidate_document(snapshot, candidate, include_bytes=download)
        if document["transfer_digest"] != row["descriptor"]["transfer_digest"]:
            raise CollectionError(
                "analysis_input_corrupt", "전송 확인 자료의 무결성이 일치하지 않습니다."
            )
        total = len(document["chunks"])
        return {
            **document,
            "chunks": document["chunks"] if download else document["chunks"][offset : offset + 20],
            "offset": 0 if download else offset,
            "total": total,
            "all_chunks_included": download or total <= 20 and offset == 0,
            "chunk_text_truncated": False,
            "media_bytes_included": download,
        }

    def transfer_media(self, project: str, preparation: str, index: int) -> tuple[str, bytes]:
        _, snapshot = self._preview_snapshot(project, preparation)
        chunks = split_evidence(snapshot, Limits(**self.profile["limits"]))
        if not 0 <= index < len(chunks) or chunks[index].media is None:
            raise CollectionError("not_found", "해당 전송 비텍스트 자료가 없습니다.")
        media = chunks[index].media
        assert media is not None
        return media.mime_type, media.raw

    @staticmethod
    def _consent(
        project: str, preparation: str, digest: str, fingerprint: str, descriptor: Row
    ) -> str:
        return sha256(
            canonical(
                {
                    "project_id": project,
                    "preparation_id": preparation,
                    "input_digest": digest,
                    "source_fingerprint": fingerprint,
                    "descriptor": descriptor,
                }
            ).encode()
        )

    def request(self, project: str, request: str) -> Row:
        return self.db.one(
            f"SELECT {REQUEST_FIELDS} FROM analysis_requests WHERE project_id=%s AND id=%s",
            (project, request),
        )

    def listing(self, project: str) -> list[Row]:
        self.db.one("SELECT id FROM projects WHERE id=%s", (project,))
        return self.db.all(
            f"SELECT {REQUEST_FIELDS} FROM analysis_requests WHERE project_id=%s "
            "ORDER BY created_at DESC,id DESC",
            (project,),
        )

    def submit(self, project: str, body: SubmitAnalysis) -> Row:
        if self.profile.get("preparation_only"):
            raise CollectionError(
                "analysis_transfer_disabled", "로컬 준비 전용이므로 분석을 접수하지 않습니다."
            )
        request, preparation = str(body.request_id), str(body.preparation_id)
        submitted = body.model_dump(mode="json")
        if submitted["relation_transition_digest"] is None:
            submitted.pop(
                "relation_transition_digest"
            )  # Preserve prior non-transition replay hashes.
        submission = sha256(canonical(submitted).encode())
        # The durable checking record resolves lost responses; it is not accepted/active.
        with self.db.connect() as conn:
            self._guard(conn)
            if not conn.execute(
                "SELECT id FROM analysis_preparations WHERE project_id=%s AND id=%s",
                (project, preparation),
            ).fetchone():
                raise CollectionError("not_found", "해당 준비 기록이 없습니다.")
            conn.execute(
                "INSERT INTO analysis_requests"
                "(id,project_id,preparation_id,submission_digest,generation) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (request, project, preparation, submission, self.generation),
            )
            existing = conn.execute(
                "SELECT * FROM analysis_requests WHERE id=%s", (request,)
            ).fetchone()
            if (
                not existing
                or str(existing["project_id"]) != project
                or existing["submission_digest"] != submission
            ):
                raise CollectionError("conflict", "같은 요청 ID의 제출 내용이 다릅니다.")
        if existing["state"] != "checking":
            return self.request(project, request)
        candidate = self._cached(preparation)
        # Serialize/verify outside the admission transaction; no Git/media I/O under locks.
        payload = json.loads(candidate.payload_json) if candidate else None
        if candidate and payload is not None:
            decode_snapshot(payload, candidate.manifest, candidate.digest)
        with self.db.connect() as conn:
            self._guard(conn)
            conn.execute("SELECT pg_advisory_xact_lock(72304214)")
            conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,))
            current = conn.execute(
                "SELECT * FROM analysis_requests WHERE id=%s FOR UPDATE", (request,)
            ).fetchone()
            assert current is not None
            if current["state"] == "checking":
                prepared = conn.execute(
                    "SELECT * FROM analysis_preparations WHERE id=%s FOR UPDATE", (preparation,)
                ).fetchone()
                assert prepared is not None
                code = self._reject_code(conn, project, body, prepared, candidate)
                if code:
                    conn.execute(
                        "UPDATE analysis_requests SET state='rejected',error_code=%s,"
                        "finished_at=clock_timestamp() WHERE id=%s",
                        (code, request),
                    )
                else:
                    assert candidate is not None and payload is not None
                    resume = prepared["resume_from_request_id"]
                    inherited = 0
                    if resume:
                        original, _, checkpoints = self._resume_source(
                            conn,
                            project,
                            str(resume),
                            str(prepared["end_collection_id"]),
                            diagnostic=bool(prepared["descriptor"].get("diagnostic_retry")),
                        )
                        inherited = original["inherited_attempts"] + original["call_seq"]
                    conn.execute(
                        "UPDATE analysis_requests SET state='queued',accepted_at=clock_timestamp(),"
                        "input_digest=%s,input_manifest=%s,input_payload=%s,consent=%s,"
                        "resume_from_request_id=%s,inherited_attempts=%s WHERE id=%s",
                        (
                            candidate.digest,
                            Jsonb(candidate.manifest),
                            Jsonb(payload, dumps=lambda _: candidate.payload_json),
                            Jsonb(
                                {
                                    "confirmed": True,
                                    "input_digest": body.input_digest,
                                    "consent_digest": body.consent_digest,
                                    "relation_recovery_digest": body.relation_recovery_digest,
                                    "relation_transition_digest": body.relation_transition_digest,
                                    "descriptor": prepared["descriptor"],
                                }
                            ),
                            resume,
                            inherited,
                            request,
                        ),
                    )
                    if resume:
                        conn.execute(
                            "INSERT INTO analysis_checkpoints(request_id,project_id,ordinal,task,"
                            "contract_digest,request_digest,response,response_digest,"
                            "origin_request_id,origin_call_seq,created_at) "
                            "SELECT %s,project_id,ordinal,task,%s,request_digest,"
                            "response,response_digest,origin_request_id,origin_call_seq,created_at "
                            "FROM analysis_checkpoints WHERE project_id=%s AND request_id=%s "
                            "AND ordinal<=%s",
                            (
                                request,
                                contract_digest(candidate.digest, self.profile),
                                project,
                                resume,
                                len(checkpoints),
                            ),
                        )
                    conn.execute(
                        "UPDATE analysis_preparations SET state='consumed' WHERE id=%s",
                        (preparation,),
                    )
        # On ambiguous commit errors the caller re-queries/re-sends this SAME ID.
        result = self.request(project, request)
        if result["accepted_at"] is not None:
            with self.cache_lock:
                self.cache.pop(preparation, None)
        return result

    def _reject_code(
        self,
        conn: psycopg.Connection[Row],
        project: str,
        body: SubmitAnalysis,
        prepared: Row,
        candidate: CandidateInput | None,
    ) -> str | None:
        if conn.execute(
            "SELECT id FROM analysis_requests "
            "WHERE state IN ('queued','running','cancel_requested') LIMIT 1"
        ).fetchone():
            return "analysis_capacity_full"
        if prepared["state"] == "consumed":
            return "analysis_preparation_consumed"
        if (
            str(prepared["generation"]) != self.generation
            or candidate is None
            or candidate.expires <= time.monotonic()
        ):
            return "analysis_preparation_lost"
        if prepared["state"] != "ready":
            return (
                "analysis_no_input"
                if prepared["state"] == "empty"
                else "analysis_preparation_not_ready"
            )
        transition = prepared["descriptor"].get("resume", {}).get("transition")
        if (
            not body.confirmed
            or body.input_digest != prepared["input_digest"]
            or body.consent_digest != prepared["consent_digest"]
            or body.relation_recovery_digest != self._recovery_digest()
            or prepared["descriptor"].get("relation_recovery_digest") != self._recovery_digest()
            or body.relation_transition_digest != (transition.get("digest") if transition else None)
        ):
            return "analysis_consent_mismatch"
        if candidate.relation_transition != transition:
            return "analysis_input_corrupt"
        if (
            candidate.digest != prepared["input_digest"]
            or candidate.manifest != prepared["descriptor"]["manifest"]
        ):
            return "analysis_input_corrupt"
        if prepared["descriptor"]["profile"] != self.profile:
            return "analysis_policy_changed"
        if prepared["descriptor"].get("transfer_policy") != TRANSFER_POLICY:
            return "analysis_policy_changed"
        if prepared["consent_digest"] != self._consent(
            project,
            str(prepared["id"]),
            candidate.digest,
            prepared["source_fingerprint"],
            prepared["descriptor"],
        ):
            return "analysis_consent_mismatch"
        fixed = decode_snapshot(
            json.loads(candidate.payload_json), candidate.manifest, candidate.digest
        )
        diagnostic = prepared["descriptor"].get("diagnostic")
        if bool(prepared["descriptor"].get("diagnostic_retry")) != bool(diagnostic):
            return "analysis_diagnostic_unavailable"
        if diagnostic and (
            diagnostic != self.diagnostic_target or candidate.diagnostic_wire is None
        ):
            return "analysis_diagnostic_unavailable"
        if bool(candidate.diagnostic_wire) != bool(diagnostic):
            return "analysis_diagnostic_unavailable"
        try:
            if self._candidate_document(fixed, candidate)["transfer_digest"] != prepared[
                "descriptor"
            ].get("transfer_digest"):
                return "analysis_input_corrupt"
        except CollectionError as exc:
            return exc.code
        try:
            version = source_version(conn, project, str(prepared["end_collection_id"]), lock=True)
        except CollectionError as exc:
            return exc.code
        if version["fingerprint"] != prepared["source_fingerprint"]:
            return "analysis_input_changed"
        if prepared["resume_from_request_id"]:
            try:
                original, original_preparation, checkpoints = self._resume_source(
                    conn,
                    project,
                    str(prepared["resume_from_request_id"]),
                    str(prepared["end_collection_id"]),
                    diagnostic=bool(diagnostic),
                )
                if (
                    candidate.digest != original["input_digest"]
                    or candidate.payload_json != canonical(original["input_payload"])
                    or prepared["source_fingerprint"] != original_preparation["source_fingerprint"]
                    or prepared["descriptor"].get("resume")
                    != self._resume_descriptor(original, checkpoints)
                ):
                    return "analysis_input_corrupt"
            except CollectionError as exc:
                return exc.code
        return None
