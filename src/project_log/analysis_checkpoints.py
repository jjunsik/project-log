"""Bounded, immutable completed responses bound to one fixed input and execution contract."""

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict

from pydantic import BaseModel

from project_log.analysis_input import canonical
from project_log.db import Database, Row
from project_log.experience_input import EXPERIENCE_INPUT_POLICY, ExperienceInput
from project_log.experience_planning import PLANNING_POLICY
from project_log.experience_relations import relation_recovery_policy, review_schema
from project_log.experiences import (
    ANALYSIS_POLICY,
    BASE_INSTRUCTION,
    FAILURE_POLICY,
    RESPONSE_SCHEMAS,
    TASK_INSTRUCTIONS,
    Chunk,
    Consolidation,
    Discovery,
    ExperienceGraph,
    Limits,
    State,
    batches,
    consolidation_schema,
    discovery_schema,
    resolve_chunks,
    split_evidence,
)
from project_log.gemini_request import request_policy
from project_log.git import CollectionError
from project_log.policy import sha256

ISOLATION_COMPATIBILITY = "validated-prefix-to-isolated-recovery-v1"
NOTICE_KEYS = ("transfer_scope", "data_policy", "notice_version")
OLD_NOTICE_DIGEST = "7ceaca44dd843faab593913f11774def19ba957b8f1a9979ef3f2844641ef8e5"
NEW_NOTICE_DIGEST = "41a9ed7b1e9db2b9fb57867baaf0fb0bdf503322f3a2fbbe386a4534754749b6"

PLANNING_COMPATIBILITY = "exact-requests-to-wire-planned-units-v1"

DURABILITY_COMPATIBILITY = "exact-request-checkpoints-to-durable-events-v1"

CHECKPOINT_POLICY = "validated-responses-v1"
LEGACY_INSTRUCTION_DIGEST = "3283043948a17334f462e6f9583fd0b968048e69259fe277038331a64935019b"
LEGACY_DISCOVERY_CONTRACT = "3c46f537a4f14d4e78c97b3e0b7efd056c1c21e3fe1137c890d79fbeff385ada"
DISCOVERY_COMPATIBILITY = "known-v5-discovery-prefix-to-global-relations-v1"
BUDGET_COMPATIBILITY = "unchanged-graph-per-execution-budget-v1"
RETRY_COMPATIBILITY = "unchanged-graph-provider-recovery-v1"
PREVIOUS_RETRYABLE = "explicit_http_503_or_bounded_minute_429"
REQUEST_COMPATIBILITY = "validated-discovery-consolidation-prefix-to-bounded-wire-v1"
RELATION_RECOVERY_COMPATIBILITY = (
    "validated-discovery-consolidation-prefix-to-grounding-recovery-v1"
)
PREVIOUS_RELATION_INSTRUCTION = "c030801553f047bc29bb9d2217e5637e7f68f21ea550524d3aee4226f1fa1020"
RELATION_FIX_COMPATIBILITY = "validated-prefix-to-grounding-contract-fix-v1"
FIXED_RELATION_INSTRUCTION = "16a720f7ed915070b31c1230cd354f5d8cd6a8f210c571aad457be49ebcf236f"
RELATION_FIX_RECOVERY_DIGEST = "e2fc8452ef2c5d55e7197edbd6e111951400eae47a27d1deef0d799f22b0a82f"


def previous_budget_profile(profile: Row) -> Row:
    """Only the explicitly superseded Free Tier execution controls may differ."""
    result = dict(profile)
    for key in ("budget_policy", "minute_retry_max_delay", "no_progress_resume_limit"):
        result.pop(key, None)
    result["retryable"] = "explicit_http_503_only"
    return result


def compatibility_policy(old: Row, new: Row) -> str | None:
    if canonical(old) == canonical(new):
        return None
    from project_log.analysis_events import DURABILITY_POLICY

    if "planning_policy" not in old and new.get("planning_policy") == PLANNING_POLICY:
        prior = {k: v for k, v in new.items() if k != "planning_policy"}
        if (
            canonical(old) == canonical(prior)
            and old.get("durability_policy") == DURABILITY_POLICY
            and old.get("instruction_digest") == FIXED_RELATION_INSTRUCTION
        ):
            return PLANNING_COMPATIBILITY
        return compatibility_policy(old, prior)
    if "durability_policy" not in old and new.get("durability_policy") == DURABILITY_POLICY:
        prior = {k: v for k, v in new.items() if k != "durability_policy"}
        if (
            canonical(old) == canonical(prior)
            and old.get("failure_policy") == FAILURE_POLICY
            and old.get("instruction_digest") == FIXED_RELATION_INSTRUCTION
        ):
            return DURABILITY_COMPATIBILITY
        return compatibility_policy(old, prior)
    if (
        "failure_policy" not in old
        and new.get("failure_policy") == FAILURE_POLICY
        and old.get("instruction_digest") == FIXED_RELATION_INSTRUCTION
        and new.get("relation_recovery") == relation_recovery_policy()
        and new.get("request_contract") == request_policy()
    ):
        prior = {k: v for k, v in new.items() if k != "failure_policy"}
        same_notice = all(old.get(k) == new.get(k) for k in NOTICE_KEYS)
        reviewed_notice = (
            sha256(canonical({k: old.get(k) for k in NOTICE_KEYS}).encode()) == OLD_NOTICE_DIGEST
            and sha256(canonical({k: new.get(k) for k in NOTICE_KEYS}).encode())
            == NEW_NOTICE_DIGEST
        )
        if (same_notice or reviewed_notice) and canonical(
            {k: v for k, v in old.items() if k not in NOTICE_KEYS}
        ) == canonical({k: v for k, v in prior.items() if k not in NOTICE_KEYS}):
            return ISOLATION_COMPATIBILITY
    if "failure_policy" not in old and new.get("failure_policy") == FAILURE_POLICY:
        # Preserve only already reviewed historical transitions. Their existing
        # stage restrictions and independent quota/recovery guards still apply.
        return compatibility_policy(old, {k: v for k, v in new.items() if k != "failure_policy"})
    if (
        old.get("instruction_digest") == PREVIOUS_RELATION_INSTRUCTION
        and new.get("instruction_digest") == FIXED_RELATION_INSTRUCTION
        and old.get("relation_recovery") == relation_recovery_policy()
        and sha256(canonical(old["relation_recovery"]).encode()) == RELATION_FIX_RECOVERY_DIGEST
        and new.get("request_contract") == request_policy()
        and canonical({**old, "instruction_digest": FIXED_RELATION_INSTRUCTION}) == canonical(new)
    ):
        return RELATION_FIX_COMPATIBILITY
    if (
        "relation_recovery" not in old
        and new.get("relation_recovery") == relation_recovery_policy()
    ):
        prior = {k: v for k, v in new.items() if k != "relation_recovery"}
        if (
            canonical(old) == canonical(prior)
            and old.get("instruction_digest") == PREVIOUS_RELATION_INSTRUCTION
            and old.get("request_contract") == request_policy()
        ):
            return RELATION_RECOVERY_COMPATIBILITY
        previous = compatibility_policy(old, prior)
        if previous == REQUEST_COMPATIBILITY:
            return REQUEST_COMPATIBILITY
        if previous == DISCOVERY_COMPATIBILITY:
            return DISCOVERY_COMPATIBILITY
    if "request_contract" in new and "request_contract" not in old:
        prior = {k: v for k, v in new.items() if k != "request_contract"}
        previous = compatibility_policy(old, prior)
        if previous in (None, RETRY_COMPATIBILITY, BUDGET_COMPATIBILITY):
            return REQUEST_COMPATIBILITY
        if previous == DISCOVERY_COMPATIBILITY:
            return DISCOVERY_COMPATIBILITY
    # No prompt/schema/input/model change: validate every completed stage under
    # its original contract before copying. Request failures remain blocked.
    if new.get("retryable") == "explicit_http_500_502_503_504_or_bounded_minute_429" and canonical(
        old
    ) == canonical({**new, "retryable": PREVIOUS_RETRYABLE}):
        return RETRY_COMPATIBILITY
    expected = previous_budget_profile(new)
    if canonical(old) == canonical(expected):
        return BUDGET_COMPATIBILITY
    expected["instruction_digest"] = LEGACY_INSTRUCTION_DIGEST
    expected.pop("consolidation_policy", None)
    expected.pop("discovery_contract", None)
    if (
        canonical(old) == canonical(expected)
        and new.get("discovery_contract") == LEGACY_DISCOVERY_CONTRACT
        and discovery_contract() == LEGACY_DISCOVERY_CONTRACT
    ):
        return DISCOVERY_COMPATIBILITY
    raise CollectionError("analysis_policy_changed", "검증된 Checkpoint 호환 전환이 아닙니다.")


def discovery_contract() -> str:
    return sha256(
        canonical(
            [
                BASE_INSTRUCTION,
                TASK_INSTRUCTIONS["discover"],
                Discovery.model_json_schema(),
                ANALYSIS_POLICY,
                EXPERIENCE_INPUT_POLICY,
                asdict(Limits()),
            ]
        ).encode()
    )


def contract_digest(digest: str, profile: Row) -> str:
    return sha256(canonical([CHECKPOINT_POLICY, digest, profile]).encode())


def invalid() -> CollectionError:
    return CollectionError(
        "analysis_checkpoint_invalid",
        "저장된 중간 결과의 순서·무결성·실행 계약이 일치하지 않습니다.",
    )


def validate_rows(rows: list[Row], digest: str, profile: Row, *, legacy: bool = False) -> None:
    if ("budget_policy" not in profile and len(rows) > profile["limits"]["max_calls"]) or sum(
        len(canonical(r["response"]).encode()) for r in rows
    ) > profile["result_max_bytes"]:
        raise invalid()
    for index, row in enumerate(rows, 1):
        if (
            row["ordinal"] != index
            or row["contract_digest"] != contract_digest(digest, profile)
            or row["task"] not in RESPONSE_SCHEMAS
            or (legacy and row["task"] == "reconcile")
            or sha256(canonical(row["response"]).encode()) != row["response_digest"]
            or len(canonical(row["response"]).encode()) > profile["result_max_bytes"]
        ):
            raise invalid()
        try:
            schema = (
                Consolidation
                if legacy and row["task"] == "consolidate"
                else RESPONSE_SCHEMAS[row["task"]]
            )
            schema.model_validate(row["response"])
        except ValueError:
            raise invalid() from None


def resume_rows(
    rows: list[Row],
    snapshot: ExperienceInput,
    old: Row,
    new: Row,
    *,
    proof_events: list[Row] | None = None,
) -> list[Row]:
    policy = compatibility_policy(old, new)
    digest = snapshot.digest
    target_contract = contract_digest(digest, new)
    validate_rows(rows, digest, old, legacy=policy == DISCOVERY_COMPATIBILITY)
    if policy in {DURABILITY_COMPATIBILITY, PLANNING_COMPATIBILITY}:
        legacy_prefix = []
        anchored = False
        for row in rows:
            if "_origin_unit_digest" not in row:
                raise invalid()
            origin = row["_origin_unit_digest"]
            if origin is None:
                if anchored:
                    raise invalid()
                legacy_prefix.append(row)
            else:
                anchored = True
                if origin != row["request_digest"]:
                    raise invalid()
        if policy == PLANNING_COMPATIBILITY:
            if proof_events:
                validate_recorded_requests(rows, snapshot, old, proof_events)
            else:
                validate_completed_prefix(rows, snapshot, old)
        elif legacy_prefix:
            validate_completed_prefix(legacy_prefix, snapshot, new)
        return [{**row, "contract_digest": target_contract} for row in rows]
    if policy == ISOLATION_COMPATIBILITY:
        validate_completed_prefix(rows, snapshot, new)
        return [{**row, "contract_digest": target_contract} for row in rows]
    if policy not in (
        DISCOVERY_COMPATIBILITY,
        REQUEST_COMPATIBILITY,
        RELATION_RECOVERY_COMPATIBILITY,
        RELATION_FIX_COMPATIBILITY,
        ISOLATION_COMPATIBILITY,
    ):
        return [{**row, "contract_digest": target_contract} for row in rows]
    prefix = []
    for row in rows:
        if row["task"] not in (
            {"discover", "consolidate"} if policy != DISCOVERY_COMPATIBILITY else {"discover"}
        ):
            break
        prefix.append(row)
    if not prefix:
        raise CollectionError(
            "analysis_resume_unavailable", "호환 가능한 완료 발견 결과가 없습니다."
        )
    original_batches = batches(
        split_evidence(snapshot, Limits(**new["limits"])), Limits(**new["limits"])
    )
    if policy in (
        REQUEST_COMPATIBILITY,
        RELATION_RECOVERY_COMPATIBILITY,
        RELATION_FIX_COMPATIBILITY,
        ISOLATION_COMPATIBILITY,
    ):
        validate_unchanged_prefix(prefix, snapshot, new, original_batches)
        return [{**row, "contract_digest": target_contract} for row in prefix]
    if len(prefix) > len(original_batches):
        raise invalid()
    discovery_prefix = [row for row in prefix if row["task"] == "discover"]
    for row, batch in zip(discovery_prefix, original_batches, strict=False):
        schema = discovery_schema(batch)
        digest = sha256(
            canonical(
                {
                    "task": "discover",
                    "instruction": BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["discover"],
                    "chunks": [c.payload() for c in batch],
                    "context": {},
                    "schema": schema.model_json_schema(),
                }
            ).encode()
        )
        response = schema.model_validate(row["response"])
        valid = {c.id for c in batch}
        if row["request_digest"] != digest or any(
            not set(s.evidence_ids) <= valid or len(s.evidence_ids) != len(set(s.evidence_ids))
            for s in response.seeds
        ):
            raise invalid()
    return [{**row, "contract_digest": target_contract} for row in prefix]


def validate_recorded_requests(
    rows: list[Row], snapshot: ExperienceInput, profile: Row, events: list[Row]
) -> None:
    """Rebuild every recorded request from immutable bytes and revalidate its response.

    This is not trust in an event label: body/schema/digest, chronological validation,
    discovery order and domain grounding are checked again, without global retrieval
    or calls. The caller independently checks original Ledger provenance.
    """
    chunks = split_evidence(snapshot, Limits(**profile["limits"]))
    original_batches = batches(chunks, Limits(**profile["limits"]))
    starts = {e["unit_digest"]: e for e in events if e["kind"] == "unit_started"}
    checked = {
        e["unit_digest"]: e for e in events if e["kind"] in {"unit_reused", "unit_validated"}
    }

    class NoDispatch:
        def complete(
            self,
            task: str,
            instruction: str,
            chunks: Sequence[Chunk],
            context: str,
            schema: type[BaseModel],
        ) -> BaseModel:
            raise invalid()  # This path only uses pure domain validators.

    graph = ExperienceGraph(NoDispatch())
    seeds = [seed for cp in rows if cp["task"] == "discover" for seed in cp["response"]["seeds"]]
    graph.seed_ids = [f"seed_{i:06d}" for i in range(len(seeds))]
    try:
        for cp in rows:
            digest, task = cp["request_digest"], cp["task"]
            start, check = starts[digest], checked[digest]
            if (
                start["phase"] != task
                or check["phase"] != task
                or start["event_seq"] >= check["event_seq"]
            ):
                raise invalid()
            data = start["data"]
            batch = resolve_chunks(chunks, data["chunk_ids"])
            if data["chunks"] != [{"id": c.id, "references": list(c.references)} for c in batch]:
                raise invalid()
            context = data["context"]
            schema: type[BaseModel]
            if task == "discover":
                ordinal = data["discovery_ordinal"]
                if (
                    type(ordinal) is not int
                    or ordinal < 1
                    or list(batch) != list(original_batches[ordinal - 1])
                    or context != {}
                ):
                    raise invalid()
                schema = discovery_schema(batch)
            elif task == "consolidate":
                schema = consolidation_schema(len(context))
            elif task == "reconcile":
                schema = review_schema(
                    [s["seed_id"] for s in context["seeds"]], [s["id"] for s in context["supports"]]
                )
                for seed in context["seeds"]:
                    expected = seeds[graph.seed_ids.index(seed["seed_id"])]
                    if any(seed[k] != v for k, v in expected.items()):
                        raise invalid()
            else:
                schema = RESPONSE_SCHEMAS[task]
            if (
                data["schema"] != schema.model_json_schema()
                or graph._request_digest(task, TASK_INSTRUCTIONS[task], batch, context, schema)
                != digest
            ):
                raise invalid()
            output = schema.model_validate(cp["response"])
            graph._validate_output(task, output, batch, context)
            if task == "reconcile":
                graph.reviews.append(output.model_dump())
    except (KeyError, ValueError, IndexError, TypeError, CollectionError):
        raise invalid() from None


def validate_completed_prefix(rows: list[Row], snapshot: ExperienceInput, profile: Row) -> None:
    """Prove the entire old sequential prefix with exact current requests, no sends."""

    class Boundary(Exception):
        pass

    class PrefixJournal(ResponseJournal):
        def load(self, index: int, task: str, digest: str) -> Row | None:
            if index == len(rows) + 1:
                raise Boundary
            return super().load(index, task, digest)

    class NoProvider:
        def complete(self, *args: object, **kwargs: object) -> BaseModel:
            raise invalid()

    def no_save(*args: object) -> None:
        raise invalid()

    try:
        ExperienceGraph(
            NoProvider(), Limits(**profile["limits"]), checkpoints=PrefixJournal(rows, no_save)
        ).run(snapshot)
    except Boundary:
        return
    except (CollectionError, ValueError):
        raise invalid() from None


def validate_unchanged_prefix(
    prefix: list[Row],
    snapshot: ExperienceInput,
    profile: Row,
    original_batches: list[tuple[Chunk, ...]],
) -> None:
    """Rebuild exact unchanged logical requests and rerun all prefix domain checks.

    Stop in the journal before the first missing response, before even planning a
    fresh call. Later relation/reconstruction results use a changed context contract.
    """

    class Boundary(Exception):
        pass

    class PrefixJournal(ResponseJournal):
        def load(self, index: int, task: str, digest: str) -> Row | None:
            if index == len(prefix) + 1:
                raise Boundary
            return super().load(index, task, digest)

    class NoProvider:
        def complete(
            self,
            task: str,
            instruction: str,
            chunks: Sequence[Chunk],
            context: str,
            schema: type[BaseModel],
        ) -> BaseModel:
            raise invalid()

    def no_save(*args: object) -> None:
        raise invalid()

    graph = ExperienceGraph(
        NoProvider(), Limits(**profile["limits"]), checkpoints=PrefixJournal(prefix, no_save)
    )
    state = State(
        chunks=tuple(c for batch in original_batches for c in batch),
        batches=original_batches,
        cursor=0,
        calls=0,
        seeds=[],
        seed_origins=[],
        groups=[],
        candidates=[],
        group_seed_ids=[],
        consolidation={},
        input_digest=snapshot.digest,
    )
    try:
        while state["cursor"] < len(original_batches):
            updated = graph._discover(state)
            state["cursor"] = updated["cursor"]
            state["seeds"] = updated["seeds"]
            state["seed_origins"] = updated["seed_origins"]
        graph._initial_consolidate(state)
    except Boundary:
        if state["calls"] != len(prefix) + 1:
            raise invalid() from None
        return
    if state["calls"] != len(prefix):
        raise invalid()


def read_rows(db: Database, project: str, request: str) -> list[Row]:
    return db.all(
        "SELECT * FROM analysis_checkpoints WHERE project_id=%s AND request_id=%s ORDER BY ordinal",
        (project, request),
    )


class ResponseJournal:
    """Child-local replay. Only the parent callback commits newly validated responses."""

    def __init__(self, rows: list[Row], save: Callable[[int, str, str, Row], None]):
        self.rows = rows
        self.persist = save

    def load(self, index: int, task: str, digest: str) -> Row | None:
        if index > len(self.rows):
            if index != len(self.rows) + 1:
                raise invalid()
            return None
        row = self.rows[index - 1]
        if row["task"] != task or row["request_digest"] != digest:
            raise invalid()
        # Detach mutable claims from the immutable journal (audit may transform claims).
        result: Row = json.loads(canonical(row["response"]))
        return result

    def save(self, index: int, task: str, digest: str, output: BaseModel) -> None:
        if index != len(self.rows) + 1:
            raise invalid()
        response = output.model_dump()
        self.persist(index, task, digest, response)
        self.rows.append({"task": task, "request_digest": digest, "response": response})

    def finish(self, calls: int) -> None:
        if calls != len(self.rows):
            raise invalid()


class UnitResponseJournal(ResponseJournal):
    """Exact-request reuse for the consented isolation policy.

    Ordinals retain completion chronology and origin, not a fictitious successful
    plan prefix. Responses from superseded recovery contexts remain in history.
    Every reused response is revalidated by the Graph against its exact request.
    """

    def load(self, index: int, task: str, digest: str) -> Row | None:
        matches = [r for r in self.rows if r["task"] == task and r["request_digest"] == digest]
        if len(matches) > 1:
            raise invalid()
        return json.loads(canonical(matches[0]["response"])) if matches else None

    def save(self, index: int, task: str, digest: str, output: BaseModel) -> None:
        if self.load(index, task, digest) is not None:
            raise invalid()
        super().save(len(self.rows) + 1, task, digest, output)

    def finish(self, calls: int) -> None:
        pass  # A logical visit may fail or replay; only successful responses are CPs.
