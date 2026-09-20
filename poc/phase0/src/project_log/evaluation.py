from itertools import combinations
from typing import Any

from project_log.models import Draft, EvidenceStatus, Golden, Judgment

METRIC_VERSION = "pilot-1"


def ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def evaluate(draft: Draft, golden: Golden, judgments: list[Judgment]) -> dict[str, Any]:
    by_claim = {j.claim_id: j for j in judgments}
    if len(by_claim) != len(judgments) or set(by_claim) != {c.id for c in draft.claims}:
        raise ValueError("Every output claim must have exactly one human judgment")
    expected = {c.question_id: c for c in golden.draft.claims}
    matched: set[str] = set()
    correct = unsupported = unknown_correct = unknown_total = severe = 0
    substantive = 0
    confusion: dict[str, dict[str, int]] = {}
    for claim in draft.claims:
        judgment = by_claim[claim.id]
        target = expected.get(claim.question_id)
        unknown = claim.evidence_status == EvidenceStatus.UNKNOWN
        substantive += not unknown
        unknown_total += unknown
        severe += judgment.severe_fabrication
        status_ok = target is not None and claim.evidence_status == target.evidence_status
        kind_ok = target is not None and claim.record_kind == target.record_kind
        fully_correct = (
            judgment.supported
            and judgment.content_correct
            and status_ok
            and kind_ok
            and not judgment.severe_fabrication
        )
        if target:
            row = confusion.setdefault(target.evidence_status.value, {})
            row[claim.evidence_status.value] = row.get(claim.evidence_status.value, 0) + 1
        if unknown:
            unknown_correct += bool(fully_correct)
        else:
            unsupported += not judgment.supported or judgment.severe_fabrication
            correct += bool(fully_correct)
            if fully_correct:
                matched.add(claim.question_id)
    important = set(golden.important_question_ids)
    true_links = {link.event_id: link.story_key for link in golden.draft.links}
    predicted = {link.event_id: link.story_key for link in draft.links}
    tp = fp = fn = 0
    for a, b in combinations(true_links, 2):
        same_truth = true_links[a] == true_links[b]
        same_prediction = a in predicted and b in predicted and predicted[a] == predicted[b]
        tp += same_truth and same_prediction
        fp += not same_truth and same_prediction
        fn += same_truth and not same_prediction
    return {
        "metric_version": METRIC_VERSION,
        "interpretation": "Human-adjudicated pilot diagnostics; no performance gate is set.",
        "extraction_precision": ratio(correct, substantive),
        "unsupported_claim_rate": ratio(unsupported, substantive),
        "important_claim_recall": ratio(len(important & matched), len(important)),
        "missing_important_claim_rate": ratio(len(important - matched), len(important)),
        "unknown_rate": ratio(unknown_total, len(expected)),
        "unknown_correctness": ratio(unknown_correct, unknown_total),
        "omitted_questions": sorted(set(expected) - {c.question_id for c in draft.claims}),
        "missing_important_questions": sorted(important - matched),
        "status_confusion_on_emitted_claims": confusion,
        "severe_fabrication_count": severe,
        "link_precision": ratio(tp, tp + fp),
        "link_recall": ratio(tp, tp + fn),
        "false_merge_pairs": fp,
        "false_split_pairs": fn,
        "unassigned_events": sorted(set(true_links) - set(predicted)),
    }


def summarize_timing(samples: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"active_ms": 0.0, "wait_ms": 0.0, "overlap_ms": 0.0, "paused_ms": 0.0}
    sessions: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        sessions.setdefault(sample["session_id"], []).append(sample)
        duration = sample["duration_ms"]
        if sample["active"]:
            totals["active_ms"] += duration
        if sample["waiting"]:
            totals["wait_ms"] += duration
        if sample["active"] and sample["waiting"]:
            totals["overlap_ms"] += duration
        if not sample["active"] and not sample["waiting"]:
            totals["paused_ms"] += duration
    uncertain = []
    for session_id, events in sessions.items():
        ordered = sorted(events, key=lambda s: s["sequence"])
        sequences = [s["sequence"] for s in ordered]
        if not ordered[-1]["closed"] or sequences != list(range(len(sequences))):
            uncertain.append(session_id)
    return {
        **totals,
        "observed_ms": sum(s["duration_ms"] for s in samples),
        "uncertain_sessions": uncertain,
        "note": "Active includes reading; active/wait overlap is counted separately. "
        "Interrupted or missing intervals are unknown, not zero. Concurrent sessions are not "
        "a single-person wall-clock measurement.",
    }
