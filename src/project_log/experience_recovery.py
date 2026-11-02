"""Two bounded passes over independent units; completed responses stay immutable."""

from typing import TYPE_CHECKING, cast

from project_log.analysis_input import canonical
from project_log.policy import sha256

if TYPE_CHECKING:
    from project_log.experiences import Candidate, ExperienceGraph, State


class ExecutionPaused(Exception):
    """Global call guard closed; preserve position rather than inventing failures."""


def isolatable(code: str) -> bool:
    # Global quota, consent, cancellation, request/wire, authentication and unknown
    # transport outcomes still stop the execution. A failed response is not a CP.
    return code in {
        "unit_attempts_exhausted",
        "unit_schema_invalid",
        "invalid_unknown_claim",
        "unsupported_claim",
        "invalid_evidence_reference",
        "invalid_media_page",
        "invalid_evidence_quote",
        "provider_blocked",
        "provider_incomplete",
        "provider_response_too_large",
        "analysis_relation_attempts_exhausted",
        "analysis_relation_budget_unknown",
        "provider_temporarily_unavailable",
        "provider_server_error_h500",
        "provider_server_error_h502",
        "provider_server_error_h504",
    } or code.startswith(
        (
            "candidate_",
            "claim_",
            "invalid_discovery_reference_",
            "provider_invalid_response",
            "provider_invalid_json",
            "provider_validation_error",
            "provider_output_parser",
        )
    )


def run_isolated(graph: "ExperienceGraph", state: "State") -> "State":
    candidates: dict[str, Candidate] = {}
    for recovery_pass in range(2):
        graph.pass_index, graph.plan_id = recovery_pass, None
        graph._emit("pass_started", "graph", recovery=recovery_pass > 0)
        try:
            graph.visited_units = set()
            state["cursor"], state["seeds"], state["seed_origins"], state["candidates"] = (
                0,
                [],
                [],
                [],
            )
            while state["cursor"] < len(state["batches"]):
                state = cast("State", {**state, **graph._discover(state)})
            # Completion chronology keeps existing global IDs stable when an earlier
            # discovery batch recovers later, including across manual Resume runs.
            ordered = sorted(
                zip(state["seeds"], state["seed_origins"], strict=True),
                key=lambda item: graph.discovery_order[item[1]],
            )
            state["seeds"] = [seed for seed, _ in ordered]
            state["seed_origins"] = [origin for _, origin in ordered]
            graph.reviews = []
            state = cast("State", {**state, **graph._consolidate(state)})
            groups = [
                {
                    "key": sha256(canonical([graph.seed_keys[key] for key in ids]).encode()),
                    "seed_ids": ids,
                    "seed_keys": [graph.seed_keys[key] for key in ids],
                }
                for ids in state["group_seed_ids"]
            ]
            graph.plan_id = sha256(canonical([state["input_digest"], groups]).encode())
            graph._emit(
                "partition",
                "reconcile",
                groups=groups,
                seeds=state["consolidation"]["seeds"],
                review_units=graph.accepted_relation_units,
                unresolved_relations=[
                    f
                    for f in graph.unit_failures.values()
                    if f["task"] in {"consolidate", "reconcile"}
                ],
            )
            pending_groups = False
            while state["groups"]:
                graph._check_stop()
                ids, group = state["group_seed_ids"][0], state["groups"][0]
                key = sha256(canonical([ids, [s.model_dump() for s in group]]).encode())
                if key in candidates:
                    candidate = candidates[key].model_copy(deep=True)
                    state["candidates"].append(candidate)
                    if graph.events:
                        proof = {**graph.candidate_proofs[candidate.id], "plan_id": graph.plan_id}
                        graph._emit("candidate_validated", "candidate", **proof, reused=True)
                    state["groups"], state["group_seed_ids"] = (
                        state["groups"][1:],
                        state["group_seed_ids"][1:],
                    )
                    continue
                graph.active_seed_ids = ids
                before = len(state["candidates"])
                state = cast("State", {**state, **graph._reconstruct(state)})
                if len(state["candidates"]) > before:
                    candidates[key] = state["candidates"][-1].model_copy(deep=True)
                else:
                    pending_groups = True
        except ExecutionPaused:
            if graph.plan_id is None:
                state["consolidation"] = {
                    "seeds": [],
                    "final_groups": [],
                    "partition_status": "pending",
                }
            else:
                # No new analysis: carry previously certified candidates only
                # when their complete membership still matches the current plan.
                by_id = dict(zip(graph.seed_ids, state["seeds"], strict=True))
                present = {c.id for c in state["candidates"]}
                for ids in state["consolidation"]["final_groups"]:
                    key = sha256(canonical([ids, [by_id[i].model_dump() for i in ids]]).encode())
                    if key in candidates and candidates[key].id not in present:
                        candidate = candidates[key].model_copy(deep=True)
                        state["candidates"].append(candidate)
                        proof = {
                            **graph.candidate_proofs[candidate.id],
                            "candidate": candidate.model_dump(),
                            "plan_id": graph.plan_id,
                        }
                        graph._emit("candidate_validated", "candidate", **proof, reused=True)
                        present.add(candidate.id)
            graph._emit("pass_finished", "graph", status="paused", stop_code=graph.halted_code)
            return state
        graph.unit_failures = {
            key: value for key, value in graph.unit_failures.items() if key in graph.visited_units
        }
        graph.recovery_passes.append(
            {
                "pass": recovery_pass,
                "seeds": state["consolidation"]["seeds"],
                "final_groups": state["consolidation"]["final_groups"],
                "validated_candidates": [
                    {"id": c.id, "seed_keys": [graph.seed_keys[key] for key in c.seed_ids]}
                    for c in state["candidates"]
                ],
                "unresolved_unit_ids": list(graph.unit_failures),
            }
        )
        graph._emit(
            "pass_finished",
            "graph",
            status="partial" if graph.unit_failures else "complete",
            candidate_ids=[c.id for c in state["candidates"]],
            unresolved_total=len(graph.unit_failures),
        )
        if (not graph.unit_failures and not pending_groups) or recovery_pass:
            break
    # A failed relation never removes a candidate or a successful relation. The
    # partition contains only validated reviews; uncertainty stays explicit.
    for candidate in state["candidates"]:
        unresolved = [
            f["id"]
            for f in graph.unit_failures.values()
            if f["task"] in {"consolidate", "reconcile"}
            and (not f["seed_ids"] or set(candidate.seed_ids).intersection(f["seed_ids"]))
        ]
        if (
            unresolved
            and "일부 후보 관계가 미해결입니다. 독립 경험 확정이 아닙니다."
            not in candidate.limitations
        ):
            candidate.limitations.append(
                "일부 후보 관계가 미해결입니다. 독립 경험 확정이 아닙니다."
            )
    return state
