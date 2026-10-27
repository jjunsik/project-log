"""App-owned supervisor. Each accepted execution gets a spawned, fenced child."""

import json
import multiprocessing
import re
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from uuid import uuid4

import psycopg

from project_log.analysis_admission import AnalysisAdmission
from project_log.analysis_child import (
    CONTROL_BYTES,
    STOP_GRACE,
    ExecutionFiles,
    ModelFactory,
    run_child,
)
from project_log.analysis_diagnostic import configured_target, normalize
from project_log.analysis_execution import ExecutionState, execution_profile
from project_log.db import Database, Row
from project_log.experience_gemini import GEMINI_REQUEST_TIMEOUT_SECONDS
from project_log.experiences import AnalysisResult
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.policy import sha256
from project_log.worker import AppOwnership


@dataclass
class Child:
    process: BaseProcess
    pipe: Connection
    project: str
    request: str
    owner: str
    started: float
    stop_at: float | None = None
    result_digest: str | None = None
    code: str | None = None
    diagnostic: bool = False


class AnalysisWorker:
    def __init__(
        self,
        db: Database,
        materials: Materials,
        ownership: AppOwnership,
        *,
        profile: Row | None = None,
        model_factory: ModelFactory | None = None,
        diagnostic_target: Row | None = None,
    ):
        self.db, self.materials, self.ownership = db, materials, ownership
        self.generation = str(uuid4())
        self.profile = json.loads(json.dumps(profile)) if profile is not None else None
        if self.profile is not None:
            if self.profile.get("mode") not in {"isolated", "live"} or (
                model_factory is not None and self.profile["mode"] != "isolated"
            ):
                raise ValueError("Injected models require isolated mode")
            expected = execution_profile(self.profile["model"], mode=self.profile["mode"])
            if any(self.profile.get(k) != v for k, v in expected.items()):
                raise ValueError("Unreviewed execution profile")
        self.diagnostic_target = (
            normalize(diagnostic_target)
            if diagnostic_target
            else (configured_target() if self.profile and self.profile["mode"] == "live" else None)
        )
        self.state = ExecutionState(
            db, ownership, self.generation, self.profile, self.diagnostic_target
        )
        self.admission = AnalysisAdmission(
            db, materials, self.generation, self.profile or {}, self.diagnostic_target
        )
        self.factory, self.files = model_factory, ExecutionFiles(materials)
        self.child: Child | None = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="project-log-analysis", daemon=True)
        self.error: str | None = None
        self.poisoned: str | None = None
        self.recovered = False

    def recover(self) -> None:
        self.state.fence()
        owners = self.db.all(
            "SELECT owner_token,child_identity FROM analysis_requests "
            "WHERE state IN ('running','cancel_requested') AND owner_token IS NOT NULL"
        )
        deadline = time.monotonic() + STOP_GRACE + 3
        while any(
            not self.files.quiet(str(row["owner_token"]), row["child_identity"]) for row in owners
        ):
            if time.monotonic() >= deadline:
                raise CollectionError(
                    "analysis_worker_not_quiet", "이전 분석 실행의 종료를 확인할 수 없습니다."
                )
            time.sleep(0.05)
        self.state.recover(code="failed_server_interrupted")
        for row in owners:
            self.files.clean(str(row["owner_token"]))
        self.recovered = True

    def start(self) -> None:
        if not self.recovered:
            raise RuntimeError("Analysis recovery must finish before dispatch")
        self.state.open()
        self.thread.start()

    def close(self) -> None:
        try:
            self.state.closing()
        except Exception:
            self.error = "종료 fence 저장이 미확인입니다. 재시작 시 정리를 확인하세요."
        finally:
            self.stop_event.set()
        if not self.recovered:
            return
        if self.thread.is_alive():
            self.thread.join(timeout=STOP_GRACE + 8)
        else:
            try:
                self.shutdown()
            except (psycopg.Error, CollectionError):
                self.error = "분석 종료 상태 저장이 미확인입니다. 재시작 시 정리를 확인하세요."
        if self.thread.is_alive() or (self.child and self.child.process.is_alive()):
            raise RuntimeError("Analysis supervisor has not stopped")

    def spawn(self, row: Row) -> None:
        assert self.profile is not None
        if self.profile.get("preparation_only"):
            raise CollectionError("analysis_transfer_disabled", "로컬 준비 전용입니다.")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        owner, project, request = str(row["owner_token"]), str(row["project_id"]), str(row["id"])
        process = context.Process(
            target=run_child,
            args=(
                self.db.dsn,
                str(self.materials.root),
                project,
                request,
                self.generation,
                owner,
                self.profile,
                child,
                self.factory,
                self.diagnostic_target,
            ),
            daemon=True,
        )
        self.child = Child(
            process,
            parent,
            project,
            request,
            owner,
            time.monotonic(),
            diagnostic=bool(row["consent"]["descriptor"].get("diagnostic")),
        )
        try:
            process.start()
        except Exception:
            self.child.code = "analysis_spawn_failed"
        finally:
            child.close()

    def stop_child(self, *, force: bool = False) -> None:
        handle = self.child
        if handle is None:
            return
        if handle.stop_at is None:
            handle.stop_at = time.monotonic()
            try:
                handle.pipe.send_bytes(b'{"event":"stop"}')
            except (OSError, ValueError):
                pass
        if force or time.monotonic() - handle.stop_at >= STOP_GRACE:
            if handle.process.is_alive():
                handle.process.terminate()
                handle.process.join(timeout=1)
            if handle.process.is_alive():
                handle.process.kill()
                handle.process.join(timeout=1)

    def messages(self, handle: Child) -> None:
        for _ in range(32):
            if not handle.pipe.poll():
                return
            try:
                message = json.loads(handle.pipe.recv_bytes(CONTROL_BYTES))
            except EOFError:
                return
            event = message.get("event")
            if event == "ready":
                identity = message["identity"]
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or not all(type(n) is int and n >= 0 for n in identity)
                ):
                    raise CollectionError("analysis_control_invalid", "잘못된 실행 identity입니다.")
                try:
                    self.state.child_ready(handle.project, handle.request, handle.owner, identity)
                    ready_code = None
                except CollectionError as exc:
                    ready_code = exc.code
                handle.pipe.send_bytes(json.dumps({"event": "ready", "code": ready_code}).encode())
            elif event == "call":
                seq, phase, digest = message["seq"], message["phase"], message["digest"]
                if (
                    not isinstance(seq, int)
                    or not isinstance(phase, str)
                    or not re.fullmatch(r"[a-z_]{1,64}", phase)
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)
                ):
                    raise CollectionError(
                        "analysis_control_invalid", "잘못된 분석 제어 메시지입니다."
                    )
                try:
                    self.state.enter_call(
                        handle.project,
                        handle.request,
                        handle.owner,
                        seq,
                        phase,
                        digest,
                        message.get("variant"),
                        message.get("feedback_code"),
                        message.get("logical_digest"),
                    )
                    code = None
                except CollectionError as exc:
                    code = exc.code
                handle.pipe.send_bytes(
                    json.dumps({"event": "grant", "seq": seq, "code": code}).encode()
                )
            elif event == "phase":
                if message["phase"] != "retry_wait":
                    raise CollectionError("analysis_control_invalid", "잘못된 분석 단계입니다.")
                self.state.phase(handle.project, handle.request, handle.owner, "retry_wait")
                handle.pipe.send_bytes(
                    json.dumps({"event": "retry_wait", "seq": message["seq"]}).encode()
                )
            elif event in {"relation_state", "relation_failed"}:
                try:
                    if event == "relation_failed":
                        self.state.relation_failed(
                            handle.project,
                            handle.request,
                            handle.owner,
                            message["seq"],
                            message["error_code"],
                        )
                        budget = {}
                    else:
                        budget = self.state.relation_state(
                            handle.project,
                            handle.request,
                            handle.owner,
                            message.get("logical_digest"),
                        )
                    code = None
                except CollectionError as exc:
                    code, budget = exc.code, {}
                handle.pipe.send_bytes(
                    json.dumps({"event": event, "code": code, "budget": budget}).encode()
                )
            elif event == "checkpoint":
                assert self.profile is not None
                raw = self.files.read(
                    handle.owner, self.profile["result_max_bytes"], suffix=".checkpoint.json"
                )
                if sha256(raw) != message["digest"]:
                    raise CollectionError(
                        "analysis_checkpoint_invalid", "중간 결과 무결성 오류입니다."
                    )
                payload = json.loads(raw)
                if payload["ordinal"] != message["ordinal"]:
                    raise CollectionError(
                        "analysis_checkpoint_invalid", "중간 결과 순서 오류입니다."
                    )
                try:
                    self.state.checkpoint(handle.project, handle.request, handle.owner, payload)
                    code = None
                except CollectionError as exc:
                    code = exc.code
                handle.pipe.send_bytes(
                    json.dumps(
                        {"event": "checkpoint", "ordinal": message["ordinal"], "code": code}
                    ).encode()
                )
            elif event == "attempt_finished":
                code, seq = message["code"], message["seq"]
                if not isinstance(seq, int) or (
                    code is not None
                    and (not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code))
                ):
                    raise CollectionError("analysis_control_invalid", "잘못된 호출 결과입니다.")
                self.state.call_finished(handle.project, handle.request, handle.owner, seq, code)
            elif event == "result":
                handle.result_digest = message["digest"]
            elif event == "error":
                code = message["code"]
                handle.code = (
                    code
                    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code)
                    else "analysis_execution_failed"
                )
            else:
                raise CollectionError("analysis_control_invalid", "잘못된 분석 제어 메시지입니다.")

    def publish(self, handle: Child) -> None:
        assert self.profile is not None
        current = self.db.one("SELECT state FROM analysis_requests WHERE id=%s", (handle.request,))
        if current["state"] in {"completed", "failed", "cancelled"}:
            return
        snapshot, result = None, None
        code = handle.code
        if code is None and handle.result_digest and handle.process.exitcode == 0:
            snapshot = self.state.load(handle.project, handle.request, handle.owner)
            raw = self.files.read(handle.owner, self.profile["result_max_bytes"])
            if sha256(raw) != handle.result_digest:
                raise CollectionError(
                    "analysis_result_invalid", "결과 파일 무결성 확인에 실패했습니다."
                )
            result = AnalysisResult.model_validate_json(raw)
        elif code is None:
            cancelled_exit = handle.stop_at is not None and handle.process.exitcode in {-15, -9, 77}
            code = (
                "analysis_stopped"
                if current["state"] == "cancel_requested" and cancelled_exit
                else "analysis_child_failed"
            )
        # Retry only transactions known to have rolled back. A commit-response error
        # is resolved by re-querying this request on the next supervisor iteration.
        for attempt in range(2):
            try:
                self.state.finish(
                    handle.project,
                    handle.request,
                    handle.owner,
                    code=code,
                    snapshot=snapshot,
                    result=result,
                )
                return
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                if attempt:
                    raise

    def once(self) -> bool:
        self.ownership.check()
        if self.child:
            with self.db.connect() as conn:
                self.state.guard(conn)
            handle = self.child
            self.messages(handle)
            row = self.db.one("SELECT state FROM analysis_requests WHERE id=%s", (handle.request,))
            if row["state"] == "cancel_requested" or handle.code:
                self.stop_child()
            if handle.process.is_alive():
                assert self.profile is not None
                # A local upper bound, separate from per-I/O timeouts. Allow the
                # configured 503 cooldown without increasing the HTTP attempt budget.
                if (
                    time.monotonic() - handle.started
                    > (1 if handle.diagnostic else self.profile["limits"]["max_calls"])
                    * (
                        GEMINI_REQUEST_TIMEOUT_SECONDS
                        + 5
                        + max(self.profile["retry_delay"], self.profile["minute_retry_max_delay"])
                        + 240  # finite reservation + usage-lock quota waits
                    )
                    + 60
                ):
                    handle.code = "analysis_deadline_exceeded"
                    self.stop_child(force=True)
                return True
            if handle.process.pid is not None:
                handle.process.join()
            self.messages(handle)
            try:
                self.publish(handle)
            except CollectionError as exc:
                self.state.finish(handle.project, handle.request, handle.owner, code=exc.code)
            self.files.clean(handle.owner)
            handle.pipe.close()
            self.child = None
            return True
        self.ownership.check()
        pending = self.db.all(
            "SELECT id,project_id FROM analysis_requests "
            "WHERE state='cancel_requested' AND owner_token IS NULL"
        )
        for row in pending:
            self.state.finish(str(row["project_id"]), str(row["id"]), None)
        if self.profile is None:
            return False
        try:
            claimed = self.state.claim()
        except CollectionError as exc:
            rows = self.db.all("SELECT id,project_id FROM analysis_requests WHERE state='queued'")
            for rejected in rows:
                self.state.finish(
                    str(rejected["project_id"]), str(rejected["id"]), None, code=exc.code
                )
            return True
        if claimed:
            self.spawn(claimed)
        return bool(claimed or pending)

    def shutdown(self) -> None:
        # Fence first; stop/kill/join before any active restrictions are released.
        try:
            self.state.closing()
        finally:
            self.stop_child(force=True)
        if self.child and self.child.process.is_alive():
            raise RuntimeError("Analysis child has not stopped")
        if self.child and self.child.process.pid is not None:
            self.child.process.join()
        try:
            self.state.recover(code=self.poisoned or "failed_server_stopped", require_owner=False)
        except CollectionError as exc:
            if exc.code != "analysis_owner_lost":
                raise
        if self.child:
            self.files.clean(self.child.owner)
            self.child.pipe.close()
            self.child = None

    def loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    if self.poisoned:
                        self.shutdown()
                        return
                    self.once()
                except Exception:
                    # Fail closed on DB/control/ownership errors; do not resume a Graph.
                    if not self.stop_event.is_set():
                        self.poisoned = "analysis_runtime_error"
                    self.error = "분석 실행을 중단하고 있습니다. DB와 앱 상태를 확인하세요."
                    self.stop_child(force=True)
                self.stop_event.wait(0.05)
        finally:
            try:
                self.shutdown()
            except Exception:
                self.error = "분석 종료 상태 저장이 미확인입니다. 재시작 시 정리를 확인하세요."
