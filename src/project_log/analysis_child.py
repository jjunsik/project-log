"""Spawn child and bounded control channel; only the parent may commit run/results."""

import fcntl
import json
import os
import queue
import stat
import threading
from collections.abc import Callable, Sequence
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from project_log.analysis_checkpoints import (
    ResponseJournal,
    UnitResponseJournal,
    read_rows,
    validate_rows,
)
from project_log.analysis_diagnostic import configured_target, validate_execution
from project_log.analysis_events import DURABILITY_POLICY, AnalysisFault, failure_details
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_snapshot import decode_snapshot
from project_log.db import Database, Row
from project_log.experience_gemini import (
    GeminiExperienceModel,
    TransientProviderError,
)
from project_log.experience_planning import PLANNING_POLICY
from project_log.experience_relations import (
    Reconciliation,
    feedback_context,
    grounding_feedback,
    relation_recovery_policy,
    relation_work_digest,
    validate_review,
)
from project_log.experiences import FAILURE_POLICY, Chunk, ExperienceGraph, ExperienceModel, Limits
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import MinuteProviderError
from project_log.gemini_free_tier import from_environment as free_tier_quota
from project_log.gemini_request import validate_plan
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.policy import sha256

ModelFactory = Callable[[Row, Callable[[str], None]], ExperienceModel]
CONTROL_BYTES = 8192
STOP_GRACE = 2.0


class ExecutionFiles:
    def __init__(self, materials: Materials):
        self.materials = materials
        self.root = materials.root / ".analysis-workers"

    def path(self, owner: str, suffix: str) -> Path:
        return self.root / (str(UUID(owner)) + suffix)

    def lock(self, owner: str, *, create: bool) -> int:
        if create:
            self.materials._directory(self.materials.root, create=True)
            self.materials._directory(self.root, create=True)
        else:
            self.materials._directory(self.materials.root)
            self.materials._directory(self.root)
        fd = os.open(
            self.path(owner, ".lock"),
            os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0),
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise OSError("Unsafe analysis lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def quiet(self, owner: str, identity: list[int] | None = None) -> bool:
        if not self.root.exists() and not self.root.is_symlink():
            if identity is not None:
                raise CollectionError(
                    "analysis_worker_identity_unknown",
                    "이전 실행 잠금 identity를 확인할 수 없습니다.",
                )
            return True
        try:
            fd = self.lock(owner, create=False)
        except FileNotFoundError:
            # A not-yet-started child cannot pass the already committed generation fence.
            if identity is not None:
                raise CollectionError(
                    "analysis_worker_identity_unknown",
                    "이전 실행 잠금 identity를 확인할 수 없습니다.",
                ) from None
            return True
        except BlockingIOError:
            return False
        info = os.fstat(fd)
        os.close(fd)
        if identity is not None and identity != [info.st_dev, info.st_ino]:
            raise CollectionError(
                "analysis_worker_identity_unknown", "이전 실행 잠금 identity가 변경됐습니다."
            )
        return True

    def read(self, owner: str, maximum: int, *, suffix: str = ".json") -> bytes:
        self.materials._directory(self.materials.root)
        self.materials._directory(self.root)
        try:
            fd = os.open(self.path(owner, suffix), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise AnalysisFault(
                "analysis_result_file_missing", "result_read", "file_missing"
            ) from None
        except OSError as exc:
            raise AnalysisFault(
                "analysis_result_file_unavailable", "result_read", "open_failed", errno=exc.errno
            ) from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > maximum
                or info.st_uid != os.getuid()
            ):
                raise AnalysisFault(
                    "analysis_result_file_invalid",
                    "result_read",
                    "file_identity_or_size",
                    measured_bytes=info.st_size,
                    limit_bytes=maximum,
                    regular=stat.S_ISREG(info.st_mode),
                    owner_matches=info.st_uid == os.getuid(),
                )
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise AnalysisFault(
                "analysis_result_file_too_large",
                "result_read",
                "bytes_gt_limit",
                measured_bytes=len(raw),
                limit_bytes=maximum,
            )
        return raw

    def failure(self, owner: str, code: str, details: Row) -> None:
        """Local fsync backstop before a DB diagnostic transaction; never raw exceptions."""
        self.materials._directory(self.root)
        raw = canonical({"code": code, **details}).encode() + b"\n"
        if len(raw) > CONTROL_BYTES:
            raw = (
                canonical(
                    {
                        "code": code,
                        "stage": "unknown",
                        "condition": "diagnostic_too_large",
                        "cause_status": "UNKNOWN",
                        "measured_bytes": len(raw),
                    }
                ).encode()
                + b"\n"
            )
        fd = os.open(
            self.path(owner, ".failure.jsonl"),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def clean(self, owner: str, *, retain_result: bool = False) -> None:
        # Only known files, after join/quiescence and terminal commit. Never recursive.
        if not self.root.exists() and not self.root.is_symlink():
            return
        self.materials._directory(self.materials.root)
        self.materials._directory(self.root)
        for suffix in (".json", ".checkpoint.json", ".lock"):
            if retain_result and suffix != ".lock":
                continue
            self.path(owner, suffix).unlink(missing_ok=True)


class Control:
    def __init__(self, pipe: Connection):
        self.pipe, self.stopped, self.done = pipe, threading.Event(), threading.Event()
        self.grants: queue.Queue[Row] = queue.Queue(maxsize=1)
        self.seq, self.phase = 0, "starting"
        self.entered_seq = 0
        self.event_seq = 0
        self.durable = False
        self.files: ExecutionFiles | None = None
        self.owner: str | None = None
        self.event_max_bytes = 32 * 1024 * 1024
        self.last_error_code: str | None = None
        self.first_digest: str | None = None
        self.relation_variant: str | None = None
        self.relation_feedback: str | None = None
        self.relation_digest: str | None = None
        self.unit_request_digest: str | None = None
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self) -> None:
        try:
            while True:
                message = json.loads(self.pipe.recv_bytes(CONTROL_BYTES))
                if message["event"] == "stop":
                    break
                self.grants.put_nowait(message)
        except (EOFError, OSError, ValueError, KeyError, TypeError, queue.Full):
            pass
        self.stopped.set()
        # A blocked SDK/CPU loop must not survive its control owner.
        if not self.done.wait(STOP_GRACE):
            os._exit(77)

    def check(self) -> None:
        if self.stopped.is_set():
            raise CollectionError("analysis_stopped", "분석 실행이 중단됐습니다.")

    def send(self, **message: Any) -> None:
        raw = canonical(message).encode()
        if len(raw) > CONTROL_BYTES:
            raise CollectionError("analysis_control_invalid", "분석 제어 메시지가 너무 큽니다.")
        self.pipe.send_bytes(raw)

    def begin(self, identity: list[int]) -> None:
        self.send(event="ready", identity=identity)
        while True:
            self.check()
            try:
                response = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if response.get("event") != "ready" or response.get("code"):
                raise CollectionError(
                    response.get("code") or "analysis_control_invalid", "실행 시작이 차단됐습니다."
                )
            self.check()
            return

    def enter(self, digest: str) -> None:
        self.check()
        if self.first_digest is not None and self.first_digest != digest:
            raise CollectionError("analysis_input_mismatch", "재시도 입력이 변경됐습니다.")
        self.first_digest = digest
        self.seq += 1
        self.send(
            event="call",
            unit_request_digest=self.unit_request_digest,
            seq=self.seq,
            phase=self.phase,
            digest=digest,
            variant=self.relation_variant if self.phase == "reconcile" else None,
            feedback_code=self.relation_feedback if self.phase == "reconcile" else None,
            logical_digest=self.relation_digest if self.phase == "reconcile" else None,
        )
        while True:
            self.check()
            try:
                response = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if response.get("event") != "grant" or response.get("seq") != self.seq:
                raise CollectionError("analysis_control_invalid", "호출 승인을 확인할 수 없습니다.")
            if response.get("code"):
                raise CollectionError(response["code"], "새 모델 호출이 차단됐습니다.")
            self.check()
            self.entered_seq = self.seq
            return

    def relation(self, code: str | None = None) -> Row:
        event = "relation_failed" if code is not None else "relation_state"
        self.send(event=event, seq=self.seq, error_code=code, logical_digest=self.relation_digest)
        while True:
            self.check()
            try:
                response = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if response.get("event") != event:
                raise CollectionError("analysis_control_invalid", "관계 복구 승인이 다릅니다.")
            if response.get("code"):
                raise CollectionError(response["code"], "관계 복구가 차단됐습니다.")
            budget: Row = response["budget"]
            if not isinstance(budget, dict):
                raise CollectionError(
                    "analysis_control_invalid", "관계 예산 응답이 유효하지 않습니다."
                )
            return budget

    def retry_wait(self, delay: float) -> None:
        # Finish/phase commits precede this ACK; the cooldown starts afterwards.
        self.record(
            "attempt_retry_scheduled",
            self.phase,
            self.unit_request_digest,
            seq=self.seq,
            code=self.last_error_code,
            delay_seconds=delay,
            reason="reviewed_transport_retry_within_shared_budget",
        )
        self.send(event="phase", phase="retry_wait", seq=self.seq)
        while True:
            self.check()
            try:
                message = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if message.get("event") != "retry_wait" or message.get("seq") != self.seq:
                raise CollectionError("analysis_control_invalid", "재시도 대기 승인이 다릅니다.")
            break
        if self.stopped.wait(delay):
            self.check()

    def record(self, kind: str, phase: str, unit: str | None = None, **data: Any) -> None:
        if not self.durable:
            return
        assert self.files is not None and self.owner is not None
        self.check()
        payload = {
            "event_seq": self.event_seq + 1,
            "kind": kind,
            "phase": phase,
            "unit_digest": unit,
            "data": data,
        }
        raw = canonical(payload).encode()
        if len(raw) > self.event_max_bytes:
            raise AnalysisFault(
                "analysis_event_too_large",
                "event_write",
                "serialized_bytes_gt_limit",
                event_kind=kind,
                measured_bytes=len(raw),
                limit_bytes=self.event_max_bytes,
            )
        path = self.files.path(self.owner, ".event.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.event_seq += 1
        self.send(event="journal", seq=self.event_seq, digest=sha256(raw))
        while True:
            self.check()
            try:
                response = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if response.get("event") != "journal" or response.get("seq") != self.event_seq:
                raise AnalysisFault("analysis_event_ack_invalid", "event_commit", "ack_mismatch")
            if response.get("code"):
                raise AnalysisFault(
                    "analysis_event_persist_failed",
                    "event_commit",
                    "commit_not_confirmed",
                    storage_code=response["code"],
                )
            path.unlink()
            return

    def checkpoint(self, files: ExecutionFiles, owner: str, maximum: int, payload: Row) -> None:
        self.check()
        raw = canonical(payload).encode()
        if len(raw) > maximum:
            raise CollectionError("analysis_checkpoint_invalid", "중간 결과 크기 제한 초과입니다.")
        path = files.path(owner, ".checkpoint.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.send(event="checkpoint", ordinal=payload["ordinal"], digest=sha256(raw))
        while True:
            self.check()
            try:
                response = self.grants.get(timeout=0.1)
            except queue.Empty:
                continue
            if (
                response.get("event") != "checkpoint"
                or response.get("ordinal") != payload["ordinal"]
            ):
                raise CollectionError(
                    "analysis_control_invalid", "중간 결과 저장을 확인할 수 없습니다."
                )
            if response.get("code"):
                raise CollectionError(response["code"], "중간 결과 저장이 차단됐습니다.")
            path.unlink()
            self.check()
            return


def gemini_model(
    profile: Row, gate: Callable[[str], None], *, stopped: threading.Event
) -> ExperienceModel:
    if profile["mode"] != "live" or not profile.get("free_tier_confirmed"):
        raise CollectionError("ai_not_configured", "명시적으로 검토된 외부 전송 설정이 필요합니다.")
    config = GeminiConfig.from_environment(free_tier_confirmed=True)
    if config.model != profile["model"]:
        raise CollectionError("analysis_policy_changed", "접수 당시 모델 설정이 변경됐습니다.")
    return GeminiExperienceModel(config, before_send=gate, quota=free_tier_quota(stopped=stopped))


class ControlledModel:
    def __init__(
        self, model: ExperienceModel, control: Control, profile: Row, diagnostic: Row | None = None
    ):
        self.model, self.control, self.profile = model, control, profile
        self.diagnostic = diagnostic
        self.attempts: dict[str, int] = {}

    def _event(self, kind: str, **data: Any) -> None:
        record = getattr(self.control, "record", None)
        if record:
            record(kind, self.control.phase, self.control.unit_request_digest, **data)

    def complete(
        self,
        task: str,
        instruction: str,
        chunks: Sequence[Chunk],
        context: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        self.control.phase, self.control.first_digest = task, None
        self.control.unit_request_digest = sha256(
            canonical(
                {
                    "task": task,
                    "instruction": instruction,
                    "chunks": [c.payload() for c in chunks],
                    "context": json.loads(context),
                    "schema": schema.model_json_schema(),
                }
            ).encode()
        )
        if (
            task == "reconcile"
            and not self.diagnostic
            and self.profile.get("relation_recovery") == relation_recovery_policy()
        ):
            return self._relation_complete(task, instruction, chunks, context, schema)
        if self.diagnostic and (task != "reconcile" or self.control.seq != 0):
            raise CollectionError("analysis_diagnostic_stopped", "진단 외 분석 호출은 차단됩니다.")
        digest = sha256(
            canonical(
                {
                    "task": task,
                    "instruction": instruction,
                    "chunks": [c.payload() for c in chunks],
                    "context": context,
                    "schema": schema.model_json_schema(),
                }
            ).encode()
        )
        attempts = 1 if self.diagnostic else self.profile["attempts_per_call"]
        bounded = self.profile.get("failure_policy") == FAILURE_POLICY
        if bounded:
            attempts -= self.attempts.get(digest, 0)
            if attempts <= 0:
                raise CollectionError(
                    "unit_attempts_exhausted", "이 항목의 호출 한도가 소진됐습니다."
                )
        for attempt in range(attempts):
            self.control.check()
            previous_seq = self.control.seq
            try:
                # The real adapter gates at actual serialized transport entry.
                # An injected offline model uses this equivalent attempt boundary.
                if not isinstance(self.model, GeminiExperienceModel):
                    self.control.enter(digest)
                if bounded:
                    self.attempts[digest] = self.attempts.get(digest, 0) + 1
                result = self.model.complete(task, instruction, chunks, context, schema)
                self.control.send(event="attempt_finished", seq=self.control.seq, code=None)
                self.control.check()
                if self.diagnostic:
                    raise CollectionError(
                        "analysis_diagnostic_response_received",
                        "진단 요청의 HTTP200 구조화 응답을 수신했습니다. "
                        "후속 분석은 실행하지 않습니다.",
                    )
                return result
            except (TransientProviderError, MinuteProviderError) as exc:
                self.control.send(
                    event="attempt_finished",
                    seq=self.control.seq,
                    code=exc.code,
                )
                if attempt + 1 == attempts:
                    raise
                delay = (
                    exc.delay
                    if isinstance(exc, MinuteProviderError)
                    else self.profile["retry_delay"]
                )
                if delay > self.profile["minute_retry_max_delay"]:
                    raise
                self.control.last_error_code = exc.code
                self.control.retry_wait(delay)
            except CollectionError as exc:
                if exc.code == "analysis_diagnostic_response_received":
                    raise
                if self.control.seq > previous_seq and self.control.entered_seq == self.control.seq:
                    self.control.send(event="attempt_finished", seq=self.control.seq, code=exc.code)
                raise
        raise AssertionError("Invalid retry policy")

    def _relation_complete(
        self,
        task: str,
        instruction: str,
        chunks: Sequence[Chunk],
        context: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        original = json.loads(context)
        self.control.relation_digest = relation_work_digest(
            instruction, [c.payload() for c in chunks], original, schema.model_json_schema()
        )
        while True:
            self.control.check()
            budget = self.control.relation()
            code = budget["feedback_code"]
            current = feedback_context(original, code) if code else original
            self.control.relation_variant = "feedback" if code else "initial"
            self.control.relation_feedback = code
            # Parent authorization fixes the permitted variant. Identical transport
            # retries retain the first durable wire digest, including across runs.
            self.control.first_digest = budget["wire_digest"]
            validate_plan(
                task,
                instruction,
                [c.payload() for c in chunks],
                current,
                schema.model_json_schema(),
            )
            digest = sha256(
                canonical(
                    {
                        "task": task,
                        "instruction": instruction,
                        "chunks": [c.payload() for c in chunks],
                        "context": canonical(current),
                        "schema": schema.model_json_schema(),
                    }
                ).encode()
            )
            previous_seq = self.control.seq
            try:
                if not isinstance(self.model, GeminiExperienceModel):
                    self.control.enter(digest)
                result = self.model.complete(task, instruction, chunks, canonical(current), schema)
                self.control.send(event="attempt_finished", seq=self.control.seq, code=None)
                self.control.check()
                try:
                    output = schema.model_validate(result.model_dump())
                except ValidationError:
                    self._event(
                        "unit_failed",
                        seq=self.control.seq,
                        code="candidate_relation_schema_invalid",
                        condition="relation_schema",
                        cause_status="CONFIRMED",
                        response=result.model_dump(),
                        context=original,
                    )
                    raise CollectionError(
                        "candidate_relation_schema_invalid", "관계 응답 구조가 유효하지 않습니다."
                    ) from None
                try:
                    assert isinstance(output, Reconciliation)
                    validate_review(output, original)
                except CollectionError as exc:
                    self._event(
                        "unit_failed",
                        seq=self.control.seq,
                        code=exc.code,
                        condition="relation_grounding",
                        cause_status="CONFIRMED",
                        response=output.model_dump(),
                        context=original,
                        variant=self.control.relation_variant,
                    )
                    if grounding_feedback(exc.code) is None:
                        raise
                    self.control.relation(exc.code)  # Durable ACK before any new send.
                    if budget["used"] + 1 >= 2:
                        raise
                    self._event(
                        "attempt_retry_scheduled",
                        seq=self.control.seq,
                        code=exc.code,
                        reason="reviewed_grounding_regeneration_within_shared_budget",
                        next_variant="feedback",
                        delay_seconds=0,
                    )
                    continue
                return output
            except (TransientProviderError, MinuteProviderError) as exc:
                self.control.send(event="attempt_finished", seq=self.control.seq, code=exc.code)
                if budget["used"] + 1 >= 2:
                    raise
                delay = (
                    exc.delay
                    if isinstance(exc, MinuteProviderError)
                    else self.profile["retry_delay"]
                )
                if delay > self.profile["minute_retry_max_delay"]:
                    raise
                self.control.last_error_code = exc.code
                self.control.retry_wait(delay)
            except CollectionError as exc:
                if self.control.seq > previous_seq and self.control.entered_seq == self.control.seq:
                    self.control.send(event="attempt_finished", seq=self.control.seq, code=exc.code)
                raise


def serialize_result(result: BaseModel, maximum: int) -> bytes:
    try:
        raw = result.model_dump_json().encode()
    except Exception as exc:
        raise AnalysisFault(
            "analysis_result_serialize_failed",
            "result_serialize",
            "unknown",
            exception_type=type(exc).__name__,
        ) from None
    if len(raw) > maximum:
        raise AnalysisFault(
            "analysis_result_too_large",
            "result_size",
            "serialized_bytes_gt_limit",
            measured_bytes=len(raw),
            limit_bytes=maximum,
        )
    return raw


def run_child(
    dsn: str,
    storage_root: str,
    project: str,
    request: str,
    generation: str,
    owner: str,
    profile: Row,
    pipe: Connection,
    factory: ModelFactory | None,
    diagnostic_target: Row | None = None,
) -> None:
    control = Control(pipe)
    files = ExecutionFiles(Materials(Database(dsn), Path(storage_root)))
    fd = -1
    stage = "child_start"
    try:
        fd = files.lock(owner, create=True)
        info = os.fstat(fd)
        control.begin([info.st_dev, info.st_ino])
        with Database(dsn).connect() as conn:
            runtime = conn.execute("SELECT * FROM analysis_runtime").fetchone()
            row = conn.execute(
                "SELECT * FROM analysis_requests WHERE project_id=%s AND id=%s", (project, request)
            ).fetchone()
        if (
            runtime
            and runtime["ready"]
            and str(runtime["generation"]) == generation
            and row
            and str(row["owner_token"]) == owner
            and row["state"] == "cancel_requested"
        ):
            raise CollectionError("analysis_stopped", "분석 취소 요청이 확인됐습니다.")
        if (
            not runtime
            or not runtime["ready"]
            or str(runtime["generation"]) != generation
            or not row
            or row["state"] != "running"
            or str(row["owner_token"]) != owner
            or str(row["generation"]) != generation
            or row["consent"]["descriptor"]["profile"] != profile
        ):
            raise CollectionError("analysis_owner_lost", "분석 실행 소유권이 종료됐습니다.")
        # A long-lived parent can still hold the previous profile while a newly
        # spawned child imports changed Source. Never send under that old consent.
        expected = execution_profile(profile["model"], mode=profile["mode"])
        if any(
            profile.get(key) != expected[key]
            for key in (
                "input_policy",
                "graph_policy",
                "transfer_policy",
                "checkpoint_policy",
                "consolidation_policy",
                "instruction_digest",
                "discovery_contract",
                "budget_policy",
                "retryable",
                "minute_retry_max_delay",
                "no_progress_resume_limit",
                "relation_recovery",
                "failure_policy",
                "durability_policy",
                "planning_policy",
            )
        ):
            raise CollectionError("analysis_policy_changed", "분석 실행 계약이 변경됐습니다.")
        snapshot = decode_snapshot(row["input_payload"], row["input_manifest"], row["input_digest"])
        diagnostic = validate_execution(row, diagnostic_target)
        if diagnostic and profile["mode"] == "live" and configured_target() != diagnostic:
            raise CollectionError(
                "analysis_diagnostic_unavailable", "진단 운영 설정이 변경됐습니다."
            )
        control.durable = profile.get("durability_policy") == DURABILITY_POLICY and not diagnostic
        control.files, control.owner, control.event_max_bytes = (
            files,
            owner,
            profile["result_max_bytes"],
        )
        stage = "checkpoint_replay"
        checkpoints = read_rows(Database(dsn), project, request)
        validate_rows(checkpoints, snapshot.digest, profile)
        with Database(dsn).connect() as conn:
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
                        "analysis_checkpoint_invalid", "중간 결과 호출 출처 불일치입니다."
                    )
        control.check()
        model = (
            factory(profile, control.enter)
            if factory
            else gemini_model(profile, control.enter, stopped=control.stopped)
        )
        if isinstance(model, GeminiExperienceModel) and control.durable:
            model.validation_failure = lambda data: control.record(
                "provider_validation_failed", control.phase, control.unit_request_digest, **data
            )
        stage = "graph"
        result = ExperienceGraph(
            ControlledModel(model, control, profile, diagnostic),
            Limits(**profile["limits"]),
            plan_requests=profile.get("planning_policy") == PLANNING_POLICY and not diagnostic,
            events=(lambda kind, phase, unit, data: control.record(kind, phase, unit, **data))
            if control.durable
            else None,
            stopped=control.stopped.is_set,
            isolate_failures=profile.get("failure_policy") == FAILURE_POLICY and not diagnostic,
            checkpoints=(
                UnitResponseJournal
                if profile.get("failure_policy") == FAILURE_POLICY and not diagnostic
                else ResponseJournal
            )(
                checkpoints,
                lambda index, task, digest, response: control.checkpoint(
                    files,
                    owner,
                    profile["result_max_bytes"],
                    {
                        "ordinal": index,
                        "task": task,
                        "request_digest": digest,
                        "response": response,
                    },
                ),
            ),
            remaining_attempts=lambda: profile["limits"]["max_calls"] - control.seq,
        ).run(snapshot)
        control.check()
        stage = "result_serialize"
        control.record("finalization_started", stage, candidate_count=len(result.candidates))
        raw = serialize_result(result, profile["result_max_bytes"])
        stage = "result_write"
        with files.path(owner, ".json").open("xb") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        control.send(event="result", digest=sha256(raw))
    except BaseException as exc:
        try:
            control.send(
                event="error",
                code=exc.code
                if isinstance(exc, CollectionError)
                else (
                    "analysis_result_write_failed"
                    if stage == "result_write"
                    else "analysis_execution_failed"
                ),
                details=failure_details(exc, stage),
            )
        except (OSError, ValueError):
            pass
    finally:
        control.done.set()
        pipe.close()
        if fd >= 0:
            os.close(fd)
