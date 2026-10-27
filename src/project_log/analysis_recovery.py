"""Metadata-only manual recovery guards; no model calls, source reads or quota writes."""

import re
from datetime import UTC, datetime, time, timedelta

import psycopg
from psycopg.types.json import Jsonb

from project_log.analysis_checkpoints import (
    DISCOVERY_COMPATIBILITY,
    RELATION_FIX_COMPATIBILITY,
    RELATION_RECOVERY_COMPATIBILITY,
    REQUEST_COMPATIBILITY,
    compatibility_policy,
)
from project_log.analysis_input import canonical
from project_log.db import Row
from project_log.experience_relations import grounding_feedback, relation_recovery_policy
from project_log.gemini_free_tier import PACIFIC
from project_log.gemini_request import request_policy
from project_log.git import CollectionError
from project_log.policy import sha256

RELATION_TRANSIENT_ERRORS = {
    "provider_temporarily_unavailable",
    "provider_minute_limit",
    "provider_server_error_h500",
    "provider_server_error_h502",
    "provider_server_error_h504",
}


def daily_reset(finished: datetime) -> datetime:
    day = finished.astimezone(PACIFIC).date() + timedelta(days=1)
    return datetime.combine(day, time.min, tzinfo=PACIFIC)


def validation_failure(code: str) -> bool:
    return (
        code
        in {
            "invalid_unknown_claim",
            "unsupported_claim",
            "invalid_evidence_reference",
            "invalid_media_page",
            "invalid_evidence_quote",
            "provider_blocked",
            "provider_incomplete",
            "provider_response_too_large",
            "analysis_input_mismatch",
        }
        or code.startswith(
            (
                "candidate_",
                "claim_",
                "analysis_checkpoint",
                "analysis_policy_",
                "analysis_wire_",
                "analysis_result_",
                "invalid_discovery_reference_",
            )
        )
        or any(
            code.startswith("provider_" + kind)
            for kind in ("invalid_response", "invalid_json", "validation_error", "output_parser")
        )
    )


def changed_request_resume(original: Row, profile: Row) -> bool:
    """Known old400 -> current bounded-wire normal analysis, never a diagnostic reset.

    This is metadata eligibility only. Admission still rebuilds the fixed input,
    checks exact prefix requests/domain responses and binds a fresh consent.
    """
    old = (original.get("consent") or {}).get("descriptor", {}).get("profile", {})
    if not (original.get("error_code") or "").startswith("provider_invalid_request"):
        return False
    if profile.get("request_contract") != request_policy():
        return False
    try:
        return compatibility_policy(old, profile) == REQUEST_COMPATIBILITY
    except CollectionError:
        return False


def changed_relation_resume(original: Row, profile: Row) -> bool:
    code = original.get("error_code") or ""
    if code != "candidate_relation_grounding_invalid" and grounding_feedback(code) is None:
        return False
    if (original.get("consent") or {}).get("descriptor", {}).get("diagnostic"):
        return False
    old = (original.get("consent") or {}).get("descriptor", {}).get("profile", {})
    try:
        policy = compatibility_policy(old, profile)
        if policy == RELATION_FIX_COMPATIBILITY:
            feedback = grounding_feedback(code)
            return bool(
                feedback
                and "_labels_" in code
                and feedback["counts"]["missing"] == 0
                and feedback["counts"]["outside"] > 0
                and feedback["counts"]["duplicate"] == 0
            )
        return policy == RELATION_RECOVERY_COMPATIBILITY
    except CollectionError:
        return False


def relation_budget(
    conn: psycopg.Connection[Row],
    row: Row,
    profile: Row,
    logical_digest: str | None = None,
    *,
    inspect_exhausted: bool = False,
) -> Row:
    """One durable identity across runs, lineages, dates and process generations."""
    if profile.get("relation_recovery") != relation_recovery_policy():
        raise CollectionError("analysis_policy_changed", "검토된 관계 복구 계약이 아닙니다.")
    if logical_digest is not None:
        if not isinstance(logical_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", logical_digest):
            raise CollectionError(
                "analysis_control_invalid", "논리 요청 Digest가 유효하지 않습니다."
            )
        prepared = conn.execute(
            "SELECT source_fingerprint FROM analysis_preparations WHERE id=%s AND project_id=%s",
            (row["preparation_id"], row["project_id"]),
        ).fetchone()
        if not prepared or not prepared["source_fingerprint"]:
            raise CollectionError(
                "analysis_relation_budget_unknown", "고정 자료 버전을 확인할 수 없습니다."
            )
        identity = sha256(
            canonical(
                [
                    prepared["source_fingerprint"],
                    profile["model"],
                    profile["relation_recovery"],
                    logical_digest,
                ]
            ).encode()
        )
    else:
        # Status can locate an unfinished call cheaply; prepare/child still validate
        # the exact immutable prefix and the actual next logical request.
        latest = conn.execute(
            "SELECT logical_digest,phase FROM analysis_calls WHERE request_id=%s AND seq>"
            "(SELECT COALESCE(max(origin_call_seq),0) FROM analysis_checkpoints "
            "WHERE request_id=%s AND origin_request_id=%s) ORDER BY seq LIMIT 1",
            (row["id"], row["id"], row["id"]),
        ).fetchone()
        if latest is None or latest["phase"] != "reconcile":
            return {"applicable": False, "used": 0, "feedback_code": None, "wire_digest": None}
        identity = latest["logical_digest"]
        if identity is None:
            raise CollectionError(
                "analysis_relation_budget_unknown", "관계 요청 식별자가 없습니다."
            )
    calls = conn.execute(
        "SELECT c.payload_digest,c.outcome,c.error_code FROM analysis_calls c "
        "JOIN analysis_requests r ON r.id=c.request_id WHERE c.project_id=%s "
        "AND c.logical_digest=%s AND c.entered_at>COALESCE("
        "(SELECT max(done.entered_at) FROM analysis_checkpoints cp JOIN analysis_calls done "
        "ON done.request_id=cp.origin_request_id AND done.seq=cp.origin_call_seq "
        "WHERE cp.project_id=%s AND done.logical_digest=%s),'-infinity'::timestamptz) "
        "ORDER BY c.entered_at,c.request_id,c.seq",
        (row["project_id"], identity, row["project_id"], identity),
    ).fetchall()
    if len(calls) >= 2:
        if inspect_exhausted:
            return {
                "used": len(calls),
                "exhausted": True,
                "known": len(calls) == 2
                and all(
                    (c["outcome"] == "responded" and grounding_feedback(c["error_code"] or ""))
                    or (c["outcome"] == "failed" and c["error_code"] in RELATION_TRANSIENT_ERRORS)
                    for c in calls
                ),
            }
        raise CollectionError(
            "analysis_relation_attempts_exhausted", "이 관계 요청의 전송 한도가 소진됐습니다."
        )
    feedback, wire = None, None
    if calls:
        call = calls[0]
        if call["outcome"] == "responded" and grounding_feedback(call["error_code"] or ""):
            feedback = call["error_code"]
        elif call["outcome"] == "failed" and call["error_code"] in RELATION_TRANSIENT_ERRORS:
            wire = call["payload_digest"]
        else:
            raise CollectionError(
                "analysis_relation_budget_unknown", "이전 관계 응답 상태를 확인할 수 없습니다."
            )
    return {
        "applicable": True,
        "used": len(calls),
        "feedback_code": feedback,
        "wire_digest": wire,
        "logical_digest": identity,
    }


def resume_block(
    conn: psycopg.Connection[Row],
    original: Row,
    profile: Row,
    *,
    now: datetime | None = None,
) -> str | None:
    if "budget_policy" not in profile:
        return None  # Historical fixture/runtime contract, never the new execution policy.
    now = now or datetime.now(UTC)
    old = (original.get("consent") or {}).get("descriptor", {}).get("profile", {})
    try:
        policy = compatibility_policy(old, profile)
    except CollectionError:
        return "analysis_policy_changed"
    code = original.get("error_code") or ""
    if code in {"analysis_relation_attempts_exhausted", "analysis_relation_budget_unknown"}:
        return code
    if policy == RELATION_FIX_COMPATIBILITY:
        if not changed_relation_resume(original, profile):
            return "analysis_resume_validation_required"
        # Preserve the old exhausted bucket; only this reviewed contract transition
        # may start the distinct new request identity. Unknown outcomes stay closed.
        try:
            budget = relation_budget(conn, original, old, inspect_exhausted=True)
        except CollectionError as exc:
            return exc.code
        if not budget.get("exhausted"):
            return "analysis_resume_validation_required"
        if not budget["known"]:
            return "analysis_relation_budget_unknown"
        source = conn.execute(
            "SELECT source_fingerprint FROM analysis_preparations WHERE id=%s",
            (original["preparation_id"],),
        ).fetchone()
        if not source or not source["source_fingerprint"]:
            return "analysis_input_corrupt"
        # Once any execution under the target contract was accepted for these fixed
        # sources, follow that execution's normal resume path. This also closes a
        # second donor, an independent run, cancellation and restart loopholes.
        if conn.execute(
            "SELECT r.id FROM analysis_requests r JOIN analysis_preparations p "
            "ON p.id=r.preparation_id WHERE r.project_id=%s AND r.accepted_at IS NOT NULL "
            "AND p.source_fingerprint=%s AND r.consent->'descriptor'->'profile'=%s LIMIT 1",
            (original["project_id"], source["source_fingerprint"], Jsonb(profile)),
        ).fetchone():
            return "analysis_relation_transition_consumed"
        return None
    if changed_relation_resume(original, profile):
        return None
    if changed_request_resume(original, profile):
        # The original's accepted diagnostic child remains consumed. A terminal
        # diagnostic may donate its validated prefix to a distinct normal child.
        # Old-contract no-progress failures do not describe this new wire contract.
        return None
    if (original.get("consent") or {}).get("descriptor", {}).get("diagnostic"):
        return "analysis_diagnostic_stopped"
    if code in {
        "analysis_request_too_large",
        "analysis_request_schema_invalid",
        "gemini_free_request_limit",
        "gemini_free_tokens_unknown",
    } or code.startswith(("provider_invalid_request", "analysis_request_contract_limit_")):
        # A date/quota reset or budget-only transition cannot repair the request.
        # A future proven request fix needs its own explicit compatibility path.
        return "analysis_resume_request_fix_required"
    if (
        validation_failure(code)
        and grounding_feedback(code) is None
        and policy != DISCOVERY_COMPATIBILITY
    ):
        return "analysis_resume_validation_required"
    if (
        profile.get("relation_recovery") == relation_recovery_policy()
        and old.get("relation_recovery") == profile["relation_recovery"]
    ):
        try:
            budget = relation_budget(conn, original, profile)
        except CollectionError as exc:
            return exc.code
        if budget["applicable"] and budget["feedback_code"]:
            # Preserve a quota/date stop below; only classified grounding errors bypass
            # the generic validation block while one authorized attempt remains.
            if grounding_feedback(code):
                return None
    if validation_failure(code) and policy != DISCOVERY_COMPATIBILITY:
        return "analysis_resume_validation_required"
    finished = original.get("finished_at")
    if finished and code == "gemini_free_daily_limit" and now < daily_reset(finished):
        return "analysis_resume_daily_wait"
    if finished and code in {"provider_minute_limit", "provider_rate_limited"}:
        if now < finished + timedelta(seconds=60):
            return "analysis_resume_minute_wait"
    # Three failed executions at the same frontier during the provider day are
    # enough to stop blind repetition. Next day still requires an explicit new
    # preparation/consent; histories and cumulative attempts never reset.
    current = original
    for _ in range(profile["no_progress_resume_limit"]):
        if (
            not current.get("finished_at")
            or current["finished_at"].astimezone(PACIFIC).date() != now.astimezone(PACIFIC).date()
            or (current.get("consent") or {})
            .get("descriptor", {})
            .get("profile", {})
            .get("instruction_digest")
            != old.get("instruction_digest")
        ):
            return None
        fresh = conn.execute(
            "SELECT count(*) AS n FROM analysis_checkpoints "
            "WHERE request_id=%s AND origin_request_id=%s",
            (current["id"], current["id"]),
        ).fetchone()
        if fresh and fresh["n"]:
            return None
        parent = current.get("resume_from_request_id")
        if not parent:
            return None
        current = (
            conn.execute(
                "SELECT id,finished_at,consent,resume_from_request_id FROM analysis_requests "
                "WHERE project_id=%s AND id=%s",
                (original["project_id"], parent),
            ).fetchone()
            or {}
        )
    return "analysis_resume_no_progress"
