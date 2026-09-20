import time
from pathlib import Path
from typing import Any

from project_log.analysis import (
    PROMPT,
    InvalidOutput,
    ProviderUnavailable,
    context_for,
    generate,
    guard_content,
    load_live_policy,
    source_fingerprint,
)
from project_log.evaluation import evaluate, summarize_timing
from project_log.models import (
    Case,
    Draft,
    EvaluationRequest,
    Golden,
    GoldenApproval,
    ReviewRequest,
    RunRequest,
    TimingSample,
    validate_draft,
    validate_golden,
)
from project_log.store import Store, digest, identifier, utc_now


class Conflict(Exception):
    pass


class Pilot:
    def __init__(self, root: Path, data_root: Path):
        self.root = root
        self.store = Store(data_root)
        self.cases = {
            case.id: case
            for path in sorted((root / "fixtures/pilot").glob("*.json"))
            if (case := Case.model_validate_json(path.read_text()))
        }

    def case(self, case_id: str) -> Case:
        identifier(case_id)
        if case_id not in self.cases:
            raise FileNotFoundError("Case not found")
        return self.cases[case_id]

    def record(self, case_id: str, kind: str, record_id: str) -> dict[str, Any]:
        case = self.case(case_id)
        record = self.store.read(case_id, kind, record_id)
        if record["case_hash"] != digest(case.model_dump(mode="json")):
            raise Conflict("Case changed; keep previous data and start a new case version")
        return record

    def append(self, case: Case, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.store.append(
            case.id,
            kind,
            {
                "case_hash": digest(case.model_dump(mode="json")),
                "project_key": case.project_key,
                **body,
            },
        )

    def state(self, case_id: str) -> dict[str, Any]:
        case = self.case(case_id)
        data: dict[str, Any] = {"case": case.model_dump(mode="json")}
        for kind in ["runs", "revisions", "reviews", "goldens", "approvals", "evaluations"]:
            data[kind] = self.store.list(case_id, kind)
        data["timing"] = summarize_timing(self.store.list(case_id, "timing"))
        completed = {r["run_id"] for r in data["revisions"]}
        failures = {r["run_id"] for r in data["runs"] if r.get("state") == "FAILED"}
        data["incomplete_runs"] = [
            r["id"]
            for r in data["runs"]
            if r.get("state") == "STARTED" and r["id"] not in completed | failures
        ]
        data["case_hash"] = digest(case.model_dump(mode="json"))
        data["fixture_changed"] = any(
            r["case_hash"] != data["case_hash"]
            for kind in ["runs", "revisions", "goldens", "approvals"]
            for r in data[kind]
        )
        return data

    def check_head(self, case: Case, revision_id: str | None) -> None:
        revisions = self.store.list(case.id, "revisions")
        current = revisions[-1]["id"] if revisions else None
        if current != revision_id:
            raise Conflict("Draft changed; reload before modifying or approving")

    def run(self, case_id: str, request: RunRequest) -> dict[str, Any]:
        case = self.case(case_id)
        with self.store.locked():
            self.check_head(case, request.base_revision_id)
            previous = None
            if request.base_revision_id:
                previous = Draft.model_validate(
                    self.record(case_id, "revisions", request.base_revision_id)["draft"]
                )
            if request.correction is not None and (not request.correction.strip() or not previous):
                raise ValueError("Correction needs a nonempty request and a previous draft")
            context = context_for(case, request, previous)
            policy = None
            if request.mode == "gemini":
                policy = load_live_policy(self.root, digest(case.model_dump(mode="json")))
                count = sum(
                    run.get("mode") == "gemini"
                    and run.get("state") == "STARTED"
                    and run.get("policy_hash") == digest(policy)
                    for key in self.cases
                    for run in self.store.list(key, "runs")
                )
                if count >= policy["max_requests"]:
                    raise ProviderUnavailable("Approved live request budget exhausted")
            started = self.append(
                case,
                "runs",
                {
                    "state": "STARTED",
                    "mode": request.mode,
                    "base_revision_id": request.base_revision_id,
                    "context": context,
                    "context_hash": digest(context),
                    "prompt": PROMPT,
                    "prompt_hash": digest(PROMPT),
                    "schema_hash": digest(Draft.model_json_schema()),
                    "source": source_fingerprint(self.root),
                    "policy": policy,
                    "policy_hash": digest(policy),
                },
            )
        before = time.monotonic()
        try:
            draft, usage, model, raw = generate(case, request, context, policy)
            validate_draft(case, draft)
            guard_content(raw)
            with self.store.locked():
                # A concurrent edit invalidates this result, not the stored previous revision.
                self.check_head(case, request.base_revision_id)
                return self.append(
                    case,
                    "revisions",
                    {
                        "run_id": started["id"],
                        "base_revision_id": request.base_revision_id,
                        "draft": draft.model_dump(mode="json"),
                        "mode": request.mode,
                        "model": model,
                        "usage": usage,
                        "raw_response": raw,
                        "latency_ms": (time.monotonic() - before) * 1000,
                        "correction": request.correction,
                        "review_status_at_creation": "REVIEW_REQUIRED",
                    },
                )
        except Exception as exc:
            with self.store.locked():
                self.append(
                    case,
                    "runs",
                    {
                        "state": "FAILED",
                        "run_id": started["id"],
                        "mode": request.mode,
                        "error_type": type(exc).__name__,
                        "raw_response": exc.raw if isinstance(exc, InvalidOutput) else None,
                        "usage": exc.usage if isinstance(exc, InvalidOutput) else None,
                        "latency_ms": (time.monotonic() - before) * 1000,
                        "note": "Failure retained; original response/exception is not logged.",
                    },
                )
            raise

    def review(self, case_id: str, request: ReviewRequest) -> dict[str, Any]:
        case = self.case(case_id)
        guard_content(request.note)
        with self.store.locked():
            revision = self.record(case_id, "revisions", request.revision_id)
            self.check_head(case, request.revision_id)
            if request.action == "CLARIFICATION" and not request.note.strip():
                raise ValueError("Record the clarification question")
            return self.append(
                case,
                "reviews",
                {
                    **request.model_dump(mode="json"),
                    "actor": "local-user",
                    "review_status": "VERIFIED"
                    if request.action == "APPROVE"
                    else "REVIEW_REQUIRED",
                    "draft_hash": digest(revision["draft"]),
                    "evidence_status_unchanged": True,
                },
            )

    def save_golden(self, case_id: str, golden: Golden) -> dict[str, Any]:
        case = self.case(case_id)
        validate_golden(case, golden)
        guard_content(golden.model_dump(mode="json"))
        with self.store.locked():
            return self.append(
                case,
                "goldens",
                {
                    "golden": golden.model_dump(mode="json"),
                    "golden_hash": digest(golden.model_dump(mode="json")),
                    "status": "CANDIDATE",
                },
            )

    def approve_golden(self, case_id: str, request: GoldenApproval) -> dict[str, Any]:
        case = self.case(case_id)
        with self.store.locked():
            candidate = self.record(case_id, "goldens", request.candidate_id)
            if candidate["golden_hash"] != request.candidate_hash:
                raise Conflict("Golden candidate changed")
            return self.append(
                case,
                "approvals",
                {
                    "candidate_id": candidate["id"],
                    "golden_hash": candidate["golden_hash"],
                    "actor": "local-user",
                    "acknowledged": request.acknowledged,
                    "status": "APPROVED_GOLDEN",
                },
            )

    def evaluate(self, case_id: str, request: EvaluationRequest) -> dict[str, Any]:
        case = self.case(case_id)
        guard_content(request.model_dump(mode="json"))
        with self.store.locked():
            revision = self.record(case_id, "revisions", request.revision_id)
            approval = self.record(case_id, "approvals", request.golden_approval_id)
            candidate = self.record(case_id, "goldens", approval["candidate_id"])
            if candidate["golden_hash"] != approval["golden_hash"]:
                raise Conflict("Golden approval integrity mismatch")
            metrics = evaluate(
                Draft.model_validate(revision["draft"]),
                Golden.model_validate(candidate["golden"]),
                request.judgments,
            )
            return self.append(
                case,
                "evaluations",
                {
                    **request.model_dump(mode="json"),
                    "metrics": metrics,
                    "golden_hash": approval["golden_hash"],
                    "revision_hash": digest(revision),
                    "mode": revision["mode"],
                    "actor": "local-user",
                    "quality_evidence": revision["mode"] != "mock",
                },
            )

    def timing(self, case_id: str, request: TimingSample) -> dict[str, Any]:
        case = self.case(case_id)
        with self.store.locked():
            if request.revision_id:
                self.record(case_id, "revisions", request.revision_id)
            payload = request.model_dump(mode="json")
            for sample in self.store.list(case_id, "timing"):
                if (sample["session_id"], sample["sequence"]) == (
                    request.session_id,
                    request.sequence,
                ):
                    if any(sample[key] != value for key, value in payload.items()):
                        raise Conflict("Conflicting timing checkpoint")
                    return sample
            return self.append(case, "timing", payload)

    def export(self, case_id: str) -> dict[str, Any]:
        data = self.state(case_id)
        data["timing_samples"] = self.store.list(case_id, "timing")
        return {"exported_at": utc_now(), "format_version": 1, **data}
