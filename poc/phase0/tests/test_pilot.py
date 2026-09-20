import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from project_log.analysis import ProviderUnavailable, context_for
from project_log.evaluation import evaluate, summarize_timing
from project_log.models import (
    Draft,
    EvaluationRequest,
    GoldenApproval,
    Judgment,
    ReviewRequest,
    RunRequest,
    TimingSample,
    validate_draft,
)
from project_log.service import Conflict, Pilot
from project_log.store import digest


def approve_for_test(pilot: Pilot, case_id: str) -> dict:
    """Simulated human action in pytest tmp_path only, never a real Golden approval."""
    candidate = pilot.save_golden(case_id, pilot.case(case_id).golden_candidate)
    return pilot.approve_golden(
        case_id,
        GoldenApproval(
            candidate_id=candidate["id"],
            candidate_hash=candidate["golden_hash"],
            acknowledged=True,
        ),
    )


def judge_all(draft: Draft) -> list[Judgment]:
    return [Judgment(claim_id=c.id, supported=True, content_correct=True) for c in draft.claims]


def test_four_fixture_cases_contain_measurement_traps(pilot: Pilot) -> None:
    assert len(pilot.cases) == 4
    first = pilot.case("pilot-01")
    assert len(first.mock_initial.claims) < len(first.golden_candidate.draft.claims)
    cause = pilot.case("pilot-02")
    assert (
        cause.mock_initial.claims[1].evidence_status
        != cause.mock_corrected.claims[1].evidence_status
    )
    failure = json.loads(pilot.case("pilot-03").evidence[0].content)
    assert failure["exit_code"] != 0 and "ZeroDivisionError" in failure["stderr"]
    assert json.loads(pilot.case("pilot-03").evidence[2].content)["exit_code"] == 0
    linked = pilot.case("pilot-04")
    assert len(linked.repositories) == 2
    assert linked.evidence[0].occurred_at != linked.evidence[1].occurred_at


def test_context_excludes_answers_and_future_evidence(pilot: Pilot) -> None:
    case = pilot.case("pilot-01")
    context = context_for(case, RunRequest(), None)
    assert set(context) == {
        "case_id",
        "project_key",
        "questions",
        "evidence",
        "events",
        "previous_draft",
        "correction_request",
    }
    assert "golden" not in json.dumps(context) and "mock" not in json.dumps(context)


def test_revision_review_restart_and_no_status_promotion(pilot: Pilot) -> None:
    first = pilot.run("pilot-02", RunRequest())
    second = pilot.run(
        "pilot-02",
        RunRequest(
            base_revision_id=first["id"],
            correction="확정 원인으로 표현하지 마세요.",
        ),
    )
    before = digest(second["draft"])
    pilot.review("pilot-02", ReviewRequest(revision_id=second["id"], action="APPROVE"))
    restarted = Pilot(pilot.root, pilot.store.root)
    state = restarted.state("pilot-02")
    assert len(state["revisions"]) == 2
    assert digest(state["revisions"][-1]["draft"]) == before
    assert state["revisions"][-1]["draft"]["claims"][1]["evidence_status"] == "HYPOTHESIS"
    assert state["reviews"][-1]["review_status"] == "VERIFIED"
    assert state["revisions"][0]["draft"] == first["draft"]
    assert not state["approvals"]


def test_stale_revision_rejected(pilot: Pilot) -> None:
    first = pilot.run("pilot-01", RunRequest())
    pilot.run("pilot-01", RunRequest(base_revision_id=first["id"], correction="검증도 포함"))
    with pytest.raises(Conflict):
        pilot.review("pilot-01", ReviewRequest(revision_id=first["id"], action="APPROVE"))
    with pytest.raises(Conflict):
        pilot.run("pilot-01", RunRequest())


def test_foreign_project_records_and_evidence_are_rejected(pilot: Pilot) -> None:
    revision = pilot.run("pilot-01", RunRequest())
    with pytest.raises(FileNotFoundError):
        pilot.review("pilot-02", ReviewRequest(revision_id=revision["id"], action="APPROVE"))
    foreign = pilot.case("pilot-01").mock_initial.model_copy(deep=True)
    foreign.claims[0].evidence_ids = ["log"]
    with pytest.raises(ValueError, match="outside"):
        validate_draft(pilot.case("pilot-01"), foreign)


def test_golden_requires_explicit_approval_and_preserves_versions(pilot: Pilot) -> None:
    case = pilot.case("pilot-01")
    candidate = pilot.save_golden(case.id, case.golden_candidate)
    revision = pilot.run(case.id, RunRequest())
    with pytest.raises(FileNotFoundError):
        pilot.evaluate(
            case.id,
            EvaluationRequest(
                revision_id=revision["id"],
                golden_approval_id=candidate["id"],
                judgments=judge_all(case.mock_initial),
                acknowledged=True,
            ),
        )
    with pytest.raises(Conflict):
        pilot.approve_golden(
            case.id,
            GoldenApproval(
                candidate_id=candidate["id"],
                candidate_hash="wrong",
                acknowledged=True,
            ),
        )
    approval = approve_for_test(pilot, case.id)
    changed = case.golden_candidate.model_copy(deep=True)
    changed.annotation_notes = "Another candidate; prior approval does not transfer"
    pilot.save_golden(case.id, changed)
    state = pilot.state(case.id)
    assert len(state["goldens"]) == 3 and len(state["approvals"]) == 1
    assert state["approvals"][0]["id"] == approval["id"]
    assert state["goldens"][-1]["id"] != approval["candidate_id"]


def test_tampered_and_partial_records_are_not_silently_accepted(pilot: Pilot) -> None:
    revision = pilot.run("pilot-01", RunRequest())
    folder = pilot.store.directory("pilot-01", "revisions")
    (folder / ".pending-interrupted").write_text('{"incomplete":')
    assert len(pilot.state("pilot-01")["revisions"]) == 1
    path = folder / f"{revision['id']}.json"
    payload = json.loads(path.read_text())
    payload["record"]["draft"]["title"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="integrity"):
        pilot.state("pilot-01")


def test_changed_fixture_invalidates_prior_approval(pilot: Pilot) -> None:
    revision = pilot.run("pilot-01", RunRequest())
    pilot.cases["pilot-01"].evidence[0].content += " altered"
    assert pilot.state("pilot-01")["fixture_changed"]
    with pytest.raises(Conflict):
        pilot.review("pilot-01", ReviewRequest(revision_id=revision["id"], action="APPROVE"))


def test_unknown_is_not_an_unfounded_answer(pilot: Pilot) -> None:
    raw = pilot.case("pilot-01").mock_corrected.model_dump(mode="json")
    raw["claims"][-1]["text"] = "They certainly used a set"
    with pytest.raises(ValidationError, match="UNKNOWN"):
        Draft.model_validate(raw)


def test_coverage_unknown_unsupported_have_distinct_denominators(pilot: Pilot) -> None:
    case = pilot.case("pilot-01")
    result = evaluate(case.mock_initial, case.golden_candidate, judge_all(case.mock_initial))
    assert result["unsupported_claim_rate"] == {"numerator": 0, "denominator": 2, "value": 0}
    assert result["important_claim_recall"]["numerator"] == 2
    assert result["important_claim_recall"]["denominator"] == 3
    assert result["unknown_rate"]["denominator"] == 4
    assert result["missing_important_questions"] == ["verification"]
    assert result["unknown_correctness"]["value"] == 1


def test_empty_output_is_not_perfect_accuracy(pilot: Pilot) -> None:
    result = evaluate(
        Draft(title="empty", claims=[], links=[]), pilot.case("pilot-01").golden_candidate, []
    )
    assert result["unsupported_claim_rate"]["value"] is None
    assert result["extraction_precision"]["value"] is None
    assert result["important_claim_recall"]["value"] == 0


def test_hypothesis_promoted_to_fact_is_counted(pilot: Pilot) -> None:
    case = pilot.case("pilot-02")
    judgments = judge_all(case.mock_initial)
    judgments[1] = Judgment(
        claim_id=case.mock_initial.claims[1].id,
        supported=False,
        content_correct=False,
        severe_fabrication=True,
    )
    result = evaluate(case.mock_initial, case.golden_candidate, judgments)
    assert result["severe_fabrication_count"] == 1
    assert result["unsupported_claim_rate"]["numerator"] == 1
    assert result["status_confusion_on_emitted_claims"]["HYPOTHESIS"]["CONFIRMED"] == 1


def test_linking_uses_relationships_not_arbitrary_story_names(pilot: Pilot) -> None:
    case = pilot.case("pilot-04")
    result = evaluate(case.mock_initial, case.golden_candidate, judge_all(case.mock_initial))
    assert result["false_merge_pairs"] == 1 and result["false_split_pairs"] == 1
    correct = case.mock_corrected.model_copy(deep=True)
    for link in correct.links:
        link.story_key = "renamed-" + link.story_key
    result = evaluate(correct, case.golden_candidate, judge_all(correct))
    assert result["link_precision"]["value"] == 1
    assert result["link_recall"]["value"] == 1


def test_invalid_judgment_set_is_rejected(pilot: Pilot) -> None:
    case = pilot.case("pilot-01")
    with pytest.raises(ValueError, match="exactly one"):
        evaluate(case.mock_initial, case.golden_candidate, [])


def test_timing_overlap_deduplication_and_uncertainty(pilot: Pilot) -> None:
    sample = TimingSample(
        session_id="page-1",
        sequence=0,
        duration_ms=1000,
        active=True,
        waiting=True,
        observed_at="2026-01-01T00:00:00Z",
    )
    first = pilot.timing("pilot-01", sample)
    assert pilot.timing("pilot-01", sample)["id"] == first["id"]
    result = pilot.state("pilot-01")["timing"]
    assert result["active_ms"] == result["wait_ms"] == result["overlap_ms"] == 1000
    assert result["uncertain_sessions"] == ["page-1"]
    pilot.timing(
        "pilot-01",
        sample.model_copy(
            update={"sequence": 1, "closed": True, "active": False, "waiting": False}
        ),
    )
    assert not pilot.state("pilot-01")["timing"]["uncertain_sessions"]
    with pytest.raises(Conflict):
        pilot.timing("pilot-01", sample.model_copy(update={"duration_ms": 900}))
    result = summarize_timing(
        [sample.model_dump(), {**sample.model_dump(), "sequence": 2, "closed": True}]
    )
    assert result["uncertain_sessions"] == ["page-1"]


def test_unprepared_live_ai_never_creates_a_call(pilot: Pilot) -> None:
    with pytest.raises(ProviderUnavailable):
        pilot.run("pilot-01", RunRequest(mode="gemini"))
    assert not pilot.state("pilot-01")["runs"]


def test_secret_blocked_before_storage_or_model(pilot: Pilot) -> None:
    pilot.cases["pilot-01"].evidence[0].content = "PROJECT_LOG_FORBIDDEN_SECRET_MARKER"
    with pytest.raises(ValueError, match="sensitive"):
        pilot.run("pilot-01", RunRequest())
    assert not pilot.state("pilot-01")["runs"]


def test_sensitive_path_blocked(pilot: Pilot) -> None:
    pilot.cases["pilot-01"].evidence[0].locator = "repo/.env.local"
    with pytest.raises(ValueError, match="Sensitive"):
        pilot.run("pilot-01", RunRequest())


def test_failure_retained_and_incomplete_run_discoverable(pilot: Pilot, monkeypatch) -> None:
    def fail(*args):
        raise RuntimeError("simulated unavailable model")

    monkeypatch.setattr("project_log.service.generate", fail)
    with pytest.raises(RuntimeError):
        pilot.run("pilot-01", RunRequest())
    state = Pilot(pilot.root, pilot.store.root).state("pilot-01")
    assert [r["state"] for r in state["runs"]] == ["STARTED", "FAILED"]
    assert not state["revisions"] and not state["incomplete_runs"]
    pilot.append(pilot.case("pilot-01"), "runs", {"state": "STARTED", "mode": "mock"})
    assert len(Pilot(pilot.root, pilot.store.root).state("pilot-01")["incomplete_runs"]) == 1


def test_api_flow_and_origin_boundary(client: TestClient) -> None:
    assert len(client.get("/api/cases").json()) == 4
    response = client.post("/api/cases/pilot-01/runs", json={"mode": "mock"})
    assert response.status_code == 200
    revision = response.json()
    assert (
        client.post(
            "/api/cases/pilot-02/reviews",
            json={
                "revision_id": revision["id"],
                "action": "APPROVE",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/cases/pilot-01/reviews",
            json={
                "revision_id": revision["id"],
                "action": "APPROVE",
            },
            headers={"Origin": "https://hostile.invalid"},
        ).status_code
        == 403
    )
    assert client.post("/api/cases/pilot-01/runs", content="{}").status_code == 415
    assert client.get("/api/cases", headers={"Host": "hostile.invalid"}).status_code == 400
    assert client.get("/api/cases/pilot-01/export").json()["format_version"] == 1


def test_store_rejects_traversal_and_external_symlink(pilot: Pilot, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pilot.store.list("../outside", "revisions")
    outside = tmp_path / "outside"
    outside.mkdir()
    (pilot.store.root / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        pilot.store.list("escape", "revisions")
