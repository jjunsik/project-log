"""Append-only execution evidence and stage-validated partial result recovery."""

import json
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from project_log.analysis_input import canonical
from project_log.db import Row
from project_log.experience_relations import partition
from project_log.experiences import SECTIONS, Candidate, Claim
from project_log.gemini_request import RequestLimit
from project_log.git import CollectionError
from project_log.policy import sha256

DURABILITY_POLICY = "independent-events-validated-candidates-v1"
KINDS = {
    "commit_outcome_resolved",
    "attempt_retry_scheduled",
    "pass_started",
    "partition",
    "pass_finished",
    "unit_started",
    "unit_reused",
    "unit_validated",
    "unit_failed",
    "unit_isolated",
    "unit_recovered",
    "candidate_started",
    "candidate_validated",
    "execution_paused",
    "graph_finished",
    "provider_validation_failed",
    "finalization_started",
    "finalization_failed",
    "execution_finished",
    "execution_interrupted",
}


class AnalysisFault(CollectionError):
    def __init__(self, code: str, stage: str, condition: str, **measurements: Any):
        super().__init__(code, "분석 처리 조건을 확인하세요. 상세 근거는 실행 이력에 보존됩니다.")
        self.details: Row = {
            "stage": stage,
            "condition": condition,
            "cause_status": "UNKNOWN" if condition == "unknown" else "CONFIRMED",
            **measurements,
        }


def failure_details(exc: BaseException, stage: str) -> Row:
    if isinstance(exc, (AnalysisFault, RequestLimit)):
        return dict(exc.details)
    return {
        "stage": stage,
        "condition": "unknown",
        "cause_status": "UNKNOWN",
        "exception_type": type(exc).__name__,
    }


def event_invalid() -> CollectionError:
    return CollectionError(
        "analysis_event_invalid", "실행 이력의 무결성·검증 조건이 일치하지 않습니다."
    )


def append_event(conn: psycopg.Connection[Row], row: Row, event: Row) -> None:
    seq, kind, phase, unit, data = (
        event.get("event_seq"),
        event.get("kind"),
        event.get("phase"),
        event.get("unit_digest"),
        event.get("data"),
    )
    if (
        type(seq) is not int
        or seq < 1
        or kind not in KINDS
        or not isinstance(phase, str)
        or not re.fullmatch(r"[a-z_]{1,64}", phase)
        or (
            unit is not None
            and (not isinstance(unit, str) or not re.fullmatch(r"[a-f0-9]{64}", unit))
        )
        or not isinstance(data, dict)
    ):
        raise event_invalid()
    digest = sha256(canonical(event).encode())
    prior = conn.execute(
        "SELECT payload_digest FROM analysis_events WHERE request_id=%s AND event_seq=%s",
        (row["id"], seq),
    ).fetchone()
    if prior:
        if prior["payload_digest"] != digest:
            raise event_invalid()
        return
    last = conn.execute(
        "SELECT COALESCE(max(event_seq),0) AS n FROM analysis_events WHERE request_id=%s",
        (row["id"],),
    ).fetchone()
    if not last or seq != last["n"] + 1:
        raise event_invalid()
    if kind == "partition":
        validate_partition(conn, row, data)
    if kind == "candidate_validated":
        validate_candidate(conn, row, data)
    conn.execute(
        "INSERT INTO analysis_events(request_id,project_id,event_seq,kind,phase,"
        "call_seq_observed,unit_digest,data,payload_digest) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            row["id"],
            row["project_id"],
            seq,
            kind,
            phase,
            row["call_seq"],
            unit,
            Jsonb(data),
            digest,
        ),
    )


def system_event(conn: psycopg.Connection[Row], row: Row, kind: str, phase: str, data: Row) -> None:
    last = conn.execute(
        "SELECT * FROM analysis_events WHERE request_id=%s ORDER BY event_seq DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if last and last["kind"] == kind and last["phase"] == phase and last["data"] == data:
        return
    append_event(
        conn,
        row,
        {
            "event_seq": (last["event_seq"] if last else 0) + 1,
            "kind": kind,
            "phase": phase,
            "unit_digest": None,
            "data": data,
        },
    )


def current_partition(conn: psycopg.Connection[Row], request: object) -> Row | None:
    return conn.execute(
        "SELECT * FROM analysis_events WHERE request_id=%s AND kind='partition' AND event_seq>"
        "COALESCE((SELECT max(event_seq) FROM analysis_events "
        "WHERE request_id=%s AND kind='pass_started'),0) "
        "ORDER BY event_seq DESC LIMIT 1",
        (request, request),
    ).fetchone()


def validate_partition(conn: psycopg.Connection[Row], row: Row, data: Row) -> None:
    try:
        checked = {
            r["unit_digest"]: r["phase"]
            for r in conn.execute(
                "SELECT unit_digest,phase FROM analysis_events WHERE request_id=%s "
                "AND kind IN ('unit_validated','unit_reused') AND data->>'pass'=%s",
                (row["id"], str(data["pass"])),
            ).fetchall()
        }
        discoveries = conn.execute(
            "SELECT cp.ordinal,cp.request_digest,cp.response,e.data FROM analysis_checkpoints cp "
            "JOIN analysis_events e ON e.request_id=cp.request_id AND "
            "e.unit_digest=cp.request_digest "
            "WHERE cp.request_id=%s AND cp.task='discover' AND e.kind='unit_started' "
            "ORDER BY cp.ordinal",
            (row["id"],),
        ).fetchall()
        seeds: list[Row] = []
        for cp in discoveries:
            if checked.get(cp["request_digest"]) != "discover":
                continue
            origin = cp["data"]["discovery_ordinal"]
            for position, seed in enumerate(cp["response"]["seeds"]):
                seeds.append(
                    {
                        "seed_id": f"seed_{len(seeds):06d}",
                        "discovery_ordinal": origin,
                        "evidence_ids": seed["evidence_ids"],
                        "title": seed["title"],
                        "seed_key": sha256(canonical([origin, position, seed]).encode()),
                    }
                )
        if seeds != data["seeds"]:
            raise event_invalid()
        reviews = []
        for digest in data["review_units"]:
            if checked.get(digest) != "reconcile":
                raise event_invalid()
            review_cp = conn.execute(
                "SELECT response FROM analysis_checkpoints WHERE request_id=%s AND "
                "task='reconcile' AND request_digest=%s",
                (row["id"], digest),
            ).fetchone()
            if not review_cp:
                raise event_invalid()
            reviews.append(review_cp["response"])
        groups = partition([s["seed_id"] for s in seeds], reviews)
        keys = {s["seed_id"]: s["seed_key"] for s in seeds}
        expected = [
            {
                "key": sha256(canonical([keys[k] for k in group]).encode()),
                "seed_ids": group,
                "seed_keys": [keys[k] for k in group],
            }
            for group in groups
        ]
        if expected != data["groups"] or data["plan_id"] != sha256(
            canonical([row["input_digest"], expected]).encode()
        ):
            raise event_invalid()
    except (KeyError, TypeError, ValueError):
        raise event_invalid() from None


def validate_candidate(conn: psycopg.Connection[Row], row: Row, data: Row) -> None:
    """A partial artifact requires immutable successful CPs for every mandatory stage."""
    try:
        candidate = Candidate.model_validate(data["candidate"])
        plan = current_partition(conn, row["id"])
        if not plan or plan["data"]["plan_id"] != data["plan_id"]:
            raise event_invalid()
        group = next(g for g in plan["data"]["groups"] if g["key"] == data["group_key"])
        if candidate.seed_ids != group["seed_ids"] or candidate.id != sha256(
            canonical(
                [
                    row["consent"]["descriptor"]["profile"]["consolidation_policy"],
                    row["input_digest"],
                    group["seed_keys"],
                ]
            ).encode()
        ):
            raise event_invalid()
        seed = next(s for s in plan["data"]["seeds"] if s["seed_id"] == candidate.seed_ids[0])
        if candidate.title != seed["title"]:
            raise event_invalid()
        pending = plan["data"].get("unresolved_relations", [])
        if any(
            not f["seed_ids"] or set(candidate.seed_ids).intersection(f["seed_ids"])
            for f in pending
        ):
            if (
                "일부 후보 관계가 미해결입니다. 독립 경험 확정이 아닙니다."
                not in candidate.limitations
            ):
                raise event_invalid()
        required = data["required_units"]
        if (
            not required["reconstruct"]
            or not required["assess"]
            or len(required["reconstruct"]) != data["expected_reconstruction_count"]
            or len(required["assess"]) != data["expected_assessment_count"]
        ):
            raise event_invalid()
        checkpoints = {}
        for task in ("reconstruct", "audit", "assess"):
            for digest in required[task]:
                cp = conn.execute(
                    "SELECT response FROM analysis_checkpoints WHERE request_id=%s "
                    "AND project_id=%s AND task=%s AND request_digest=%s",
                    (row["id"], row["project_id"], task, digest),
                ).fetchone()
                checked = conn.execute(
                    "SELECT 1 FROM analysis_events WHERE request_id=%s "
                    "AND phase=%s AND unit_digest=%s "
                    "AND kind IN ('unit_validated','unit_reused') LIMIT 1",
                    (row["id"], task, digest),
                ).fetchone()
                if not cp or not checked:
                    raise event_invalid()
                checkpoints[digest] = cp["response"]
        pairs = data["audit_pairs"]
        if [p["reconstruct"] for p in pairs] != required["reconstruct"]:
            raise event_invalid()
        audits = []
        expected_claims = []
        expected_notes = []
        references: set[str] = set()
        for pair in pairs:
            claims = checkpoints[pair["reconstruct"]]["claims"]
            start = conn.execute(
                "SELECT data FROM analysis_events WHERE request_id=%s AND phase='reconstruct' "
                "AND kind='unit_started' AND unit_digest=%s ORDER BY event_seq LIMIT 1",
                (row["id"], pair["reconstruct"]),
            ).fetchone()
            if not start:
                raise event_invalid()
            references.update(ref for c in start["data"]["chunks"] for ref in c["references"])
            if claims:
                slices = pair.get("audits")
                if slices is not None and pair["audit"] is not None:
                    raise event_invalid()
                slices = (
                    slices
                    if slices is not None
                    else [{"unit": pair["audit"], "start": 0, "count": len(claims)}]
                )
                cursor = 0
                reviewed: list[tuple[int, Row]] = []
                for part in slices:
                    unit, begin, count = part["unit"], part["start"], part["count"]
                    if (
                        type(begin) is not int
                        or type(count) is not int
                        or begin != cursor
                        or count <= 0
                        or begin + count > len(claims)
                        or unit not in required["audit"]
                    ):
                        raise event_invalid()
                    findings = checkpoints[unit]["findings"]
                    if sorted(f["claim_index"] for f in findings) != list(range(count)):
                        raise event_invalid()
                    audit_start = conn.execute(
                        "SELECT data FROM analysis_events WHERE request_id=%s AND phase='audit' "
                        "AND kind='unit_started' AND unit_digest=%s ORDER BY event_seq LIMIT 1",
                        (row["id"], unit),
                    ).fetchone()
                    if (
                        not audit_start
                        or audit_start["data"]["context"] != claims[begin : begin + count]
                        or audit_start["data"]["chunk_ids"] != start["data"]["chunk_ids"]
                    ):
                        raise event_invalid()
                    reviewed.extend(
                        (begin + finding["claim_index"], finding) for finding in findings
                    )
                    cursor += count
                    audits.append(unit)
                if cursor != len(claims):
                    raise event_invalid()
                for claim_index, finding in reviewed:
                    claim = claims[claim_index]
                    if not finding["supported"] and claim["status"] != "UNKNOWN":
                        expected_notes.append(
                            "채택하지 않은 AI 주장: "
                            + claim["statement"]
                            + " / 검사 이유: "
                            + finding["explanation"]
                        )
                        claim = Claim(
                            section=claim["section"],
                            statement="UNKNOWN: " + finding["explanation"],
                            status="UNKNOWN",
                            citations=[],
                        ).model_dump()
                    if claim not in expected_claims:
                        expected_claims.append(claim)
            elif pair["audit"] is not None or pair.get("audits"):
                raise event_invalid()
        if audits != required["audit"]:
            raise event_invalid()
        for section in SECTIONS:
            if not any(c["section"] == section for c in expected_claims):
                expected_claims.append(
                    Claim(
                        section=section,
                        statement="UNKNOWN: 해당 내용의 근거를 확인하지 못했습니다.",
                        status="UNKNOWN",
                        citations=[],
                    ).model_dump()
                )
        assessed = []
        for digest in required["assess"]:
            start = conn.execute(
                "SELECT data FROM analysis_events WHERE request_id=%s AND phase='assess' "
                "AND kind='unit_started' AND unit_digest=%s ORDER BY event_seq LIMIT 1",
                (row["id"], digest),
            ).fetchone()
            if not start or start["data"]["context"]["title"] != candidate.title:
                raise event_invalid()
            assessed.extend(start["data"]["context"]["claims"])
        category = (
            "strong"
            if any(checkpoints[d]["category"] == "strong" for d in required["assess"])
            and any(c["status"] == "EVIDENCED" for c in expected_claims)
            else "potential"
        )
        if (
            candidate.claims != [Claim.model_validate(c) for c in expected_claims]
            or assessed != expected_claims
            or candidate.grounding_notes != expected_notes
            or candidate.evidence_ids != sorted(references)
            or candidate.category != category
            or candidate.rationale
            != "\n".join(checkpoints[d]["rationale"] for d in required["assess"])
        ):
            raise event_invalid()
    except (KeyError, StopIteration, TypeError, ValueError):
        raise event_invalid() from None


def validated_candidates(conn: psycopg.Connection[Row], row: Row) -> list[Row]:
    plan = current_partition(conn, row["id"])
    if plan is None:
        return []
    rows = conn.execute(
        "SELECT data FROM analysis_events WHERE request_id=%s AND kind='candidate_validated' "
        "AND data->>'plan_id'=%s AND event_seq>%s ORDER BY event_seq",
        (row["id"], plan["data"]["plan_id"], plan["event_seq"]),
    ).fetchall()
    result: dict[str, Row] = {}
    for item in rows:
        validate_candidate(conn, row, item["data"])
        candidate: Row = item["data"]["candidate"]
        result[candidate["id"]] = candidate
    return list(result.values())


def fallback_result(conn: psycopg.Connection[Row], row: Row, code: str) -> Any:
    """No model/reanalysis: materialize only already certified complete candidates."""
    from project_log.analysis_snapshot import decode_snapshot
    from project_log.experiences import AnalysisResult, Limits, resolve_chunks, split_evidence

    candidates = validated_candidates(conn, row)
    if not candidates:
        return None
    profile = row["consent"]["descriptor"]["profile"]
    snapshot = decode_snapshot(row["input_payload"], row["input_manifest"], row["input_digest"])
    plan = current_partition(conn, row["id"])
    assert plan is not None
    summary = conn.execute(
        "SELECT data FROM analysis_events WHERE request_id=%s AND kind='graph_finished' "
        "AND event_seq>%s ORDER BY event_seq DESC LIMIT 1",
        (row["id"], plan["event_seq"]),
    ).fetchone()
    unresolved = summary["data"].get("unresolved", []) if summary else []
    chunks = split_evidence(snapshot, Limits(**profile["limits"]))
    used = [
        c["evidence_id"]
        for candidate in candidates
        for claim in candidate["claims"]
        for c in claim["citations"]
    ]
    chunks = tuple({c.id: c for c in [*chunks, *resolve_chunks(chunks, used)]}.values())
    # Full history/failed responses stay in independently paged events, not this blob.
    coverage = {
        "analysis_status": "partial",
        "failure_policy": profile["failure_policy"],
        "stop_code": code,
        "history_request_id": str(row["id"]),
        "finalization_fallback": True,
        "events_available": True,
        "graph_status": summary["data"]["status"] if summary else "UNKNOWN",
        "unresolved_total": len(unresolved) + 1,
        "pending_work": summary["data"].get("pending_work") if summary else None,
        "unresolved": [
            {
                "id": "finalization",
                "task": "finalization",
                "code": code,
                "seed_ids": [],
                "chunk_ids": [],
            }
        ]
        + unresolved[:100],
        "recovered_unit_ids": [],
        "failure_history": [],
        "recovery_passes": [],
        "collections": [str(c["id"]) for c in json.loads(snapshot.collections_json)],
        "input_entries": len(snapshot.entries),
        "discovery_chunks": len(chunks),
        "consolidation": {
            "seeds": plan["data"]["seeds"],
            "final_groups": [g["seed_ids"] for g in plan["data"]["groups"]],
        },
        "offset_basis": "prepared_input",
        "excluded": [
            {"reference": e.reference, "reason": e.reason} for e in snapshot.entries if e.reason
        ],
        "limitations": [
            {"reference": e.reference, "reasons": list(e.limitations)}
            for e in snapshot.entries
            if e.limitations
        ],
        "chunks": [
            {
                "id": c.id,
                "references": list(c.references),
                "offset": c.offset,
                "media": c.media.descriptor() if c.media else None,
            }
            for c in chunks
        ],
    }
    result = AnalysisResult(
        input_digest=snapshot.digest,
        policy=profile["graph_policy"],
        candidates=[Candidate.model_validate(c) for c in candidates],
        coverage=coverage,
        model_calls=row["call_seq"],
    )
    measured = len(result.model_dump_json().encode())
    if measured > profile["result_max_bytes"]:
        raise AnalysisFault(
            "analysis_partial_result_too_large",
            "partial_result",
            "serialized_bytes_gt_limit",
            measured_bytes=measured,
            limit_bytes=profile["result_max_bytes"],
        )
    return snapshot, result
