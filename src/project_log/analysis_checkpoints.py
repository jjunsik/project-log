"""Bounded, immutable completed responses bound to one fixed input and execution contract."""

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict

from pydantic import BaseModel

from project_log.analysis_input import canonical
from project_log.db import Database, Row
from project_log.experience_input import EXPERIENCE_INPUT_POLICY, ExperienceInput
from project_log.experience_relations import relation_recovery_policy
from project_log.experiences import (
    ANALYSIS_POLICY,
    BASE_INSTRUCTION,
    RESPONSE_SCHEMAS,
    TASK_INSTRUCTIONS,
    Chunk,
    Consolidation,
    Discovery,
    ExperienceGraph,
    Limits,
    State,
    batches,
    discovery_schema,
    split_evidence,
)
from project_log.gemini_request import request_policy
from project_log.git import CollectionError
from project_log.policy import sha256

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


def resume_rows(rows: list[Row], snapshot: ExperienceInput, old: Row, new: Row) -> list[Row]:
    policy = compatibility_policy(old, new)
    validate_rows(rows, snapshot.digest, old, legacy=policy == DISCOVERY_COMPATIBILITY)
    if policy not in (
        DISCOVERY_COMPATIBILITY,
        REQUEST_COMPATIBILITY,
        RELATION_RECOVERY_COMPATIBILITY,
        RELATION_FIX_COMPATIBILITY,
    ):
        return [{**row, "contract_digest": contract_digest(snapshot.digest, new)} for row in rows]
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
    ):
        validate_unchanged_prefix(prefix, snapshot, new, original_batches)
        return [{**row, "contract_digest": contract_digest(snapshot.digest, new)} for row in prefix]
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
    return [{**row, "contract_digest": contract_digest(snapshot.digest, new)} for row in prefix]


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
