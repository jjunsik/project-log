"""Operator-pinned, single-request diagnosis; ordinary recovery remains blocked."""

import json
import os
import stat
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from project_log.analysis_input import canonical
from project_log.db import Row
from project_log.experience_input import ExperienceInput
from project_log.gemini import HOST, REVIEWED_MODELS, GeminiConfig
from project_log.git import CollectionError
from project_log.policy import sha256

POLICY = "pinned-reconcile-diagnostic-one-attempt-v1"
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class DiagnosticTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: Literal["pinned-reconcile-diagnostic-one-attempt-v1"] = (
        "pinned-reconcile-diagnostic-one-attempt-v1"
    )
    project_id: UUID
    request_id: UUID
    input_digest: Digest
    original_profile_digest: Digest
    wire_digest: Digest
    wire_bytes: int = Field(ge=1, le=20 * 1024 * 1024)
    checkpoint_count: int = Field(ge=1)
    call_seq: int = Field(ge=1, le=512)
    model: str
    api_version: Literal["v1beta"] = "v1beta"
    phase: Literal["reconcile"] = "reconcile"


def unavailable() -> CollectionError:
    return CollectionError(
        "analysis_diagnostic_unavailable",
        "운영자가 고정한 해당 실패 요청만 1회 진단할 수 있습니다.",
    )


def normalize(target: Row) -> Row:
    try:
        value = DiagnosticTarget.model_validate_json(canonical(target)).model_dump(mode="json")
        if value["model"] not in REVIEWED_MODELS:
            raise ValueError
        return value
    except ValueError:
        raise unavailable() from None


def configured_target() -> Row | None:
    """No key/source reads. A private local file authorizes ONE observed failure."""
    filename = os.environ.get("PROJECT_LOG_GEMINI_DIAGNOSTIC_TARGET_FILE")
    if not filename:
        return None
    descriptor = -1
    try:
        if not Path(filename).is_absolute():
            raise ValueError
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= 8192
        ):
            raise ValueError
        return normalize(json.loads(os.read(descriptor, 8193)))
    except (OSError, ValueError, TypeError, CollectionError):
        return None  # Invalid/missing authorization never enables an exception.
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def endpoint(target: Row) -> str:
    return f"https://{HOST}/v1beta/models/{target['model']}:generateContent"


def validate_target(conn: psycopg.Connection[Row], original: Row, target: Row | None) -> Row:
    if not target:
        raise unavailable()
    target = normalize(target)
    if (
        str(original["id"]) != target["request_id"]
        or str(original["project_id"]) != target["project_id"]
        or original["state"] != "failed"
        or original["accepted_at"] is None
        or not (original.get("error_code") or "").startswith("provider_invalid_request")
        or original["call_seq"] != target["call_seq"]
        or original["input_digest"] != target["input_digest"]
        or sha256(canonical(original["consent"]["descriptor"]["profile"]).encode())
        != target["original_profile_digest"]
        or original["consent"]["descriptor"]["profile"]["model"] != target["model"]
    ):
        raise unavailable()
    call = conn.execute(
        "SELECT phase,payload_digest,outcome,error_code FROM analysis_calls "
        "WHERE request_id=%s AND seq=%s",
        (original["id"], target["call_seq"]),
    ).fetchone()
    count = conn.execute(
        "SELECT count(*) AS n FROM analysis_checkpoints WHERE request_id=%s",
        (original["id"],),
    ).fetchone()
    if (
        not call
        or call["phase"] != "reconcile"
        or call["outcome"] != "failed"
        or not (call["error_code"] or "").startswith("provider_invalid_request")
        or call["payload_digest"] != target["wire_digest"]
        or not count
        or count["n"] != target["checkpoint_count"]
    ):
        raise unavailable()
    return target


def validate_execution(row: Row, target: Row | None) -> Row | None:
    diagnostic = row["consent"]["descriptor"].get("diagnostic")
    if bool(row["consent"]["descriptor"].get("diagnostic_retry")) != bool(diagnostic):
        raise unavailable()
    if not diagnostic:
        return None
    if (
        not target
        or diagnostic != target
        or str(row["project_id"]) != target["project_id"]
        or str(row["resume_from_request_id"]) != target["request_id"]
        or row["input_digest"] != target["input_digest"]
        or row["consent"]["descriptor"]["profile"]["model"] != target["model"]
    ):
        raise unavailable()
    return dict(diagnostic)


def wire_document(raw: bytes, target: Row) -> Row:
    if len(raw) != target["wire_bytes"] or sha256(raw) != target["wire_digest"]:
        raise CollectionError(
            "analysis_diagnostic_wire_mismatch",
            "기존 실패 요청과 발신 payload가 달라 차단했습니다.",
        )
    return {
        "policy": POLICY,
        "endpoint": endpoint(target),
        "model": target["model"],
        "wire_digest": target["wire_digest"],
        "wire_bytes": len(raw),
        "max_new_calls": 1,
        "automatic_retries": 0,
        "continue_analysis": False,
        "request": json.loads(raw),
    }


def capture_request(snapshot: ExperienceInput, rows: list[Row], profile: Row, target: Row) -> bytes:
    """Replay verified responses and capture native SDK bytes without a credential/network."""
    from project_log.analysis_checkpoints import ResponseJournal
    from project_log.experience_gemini import GeminiExperienceModel
    from project_log.experiences import ExperienceGraph, Limits

    captured: list[bytes] = []

    class Captured(BaseException):
        pass

    class Capture(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method != "POST" or str(request.url) != endpoint(target):
                raise unavailable()
            wire_document(request.content, target)
            captured.append(request.content)
            raise Captured

    def no_save(*args: object) -> None:
        raise unavailable()

    model = GeminiExperienceModel(
        GeminiConfig(profile["model"], "synthetic-offline-diagnostic-capture", True),
        test_transport_factory=Capture,
    )
    try:
        ExperienceGraph(
            model,
            Limits(**profile["limits"]),
            checkpoints=ResponseJournal(rows, no_save),
            remaining_attempts=lambda: 1,
        ).run(snapshot)
    except Captured:
        pass
    if len(captured) != 1:
        raise unavailable()
    return captured[0]
