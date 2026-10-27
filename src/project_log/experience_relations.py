"""Bounded global retrieval and model-reviewed relations, never heuristic merging."""

import math
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from project_log.analysis_input import canonical
from project_log.git import CollectionError
from project_log.policy import sha256

RELATION_POLICY = "global-retrieval-partition-review-v2-lossless-sources-pair-cover"
CORE_SEEDS = 32
WINDOW_SEEDS = 64
EXCERPT_CHARS = 240
RELATION_CONTEXT_CHARS = 96000
GROUND_LABELS_PATTERN = (
    r"candidate_relation_grounding_(merge|split)_labels_m([0-9]+)_x([0-9]+)_d([0-9]+)"
)
GROUND_REFS_PATTERN = (
    r"candidate_relation_grounding_(merge|split)_refs_d([0-9]+)_e([0-9]+)_x([0-9]+)_u([0-9]+)"
)


def relation_recovery_policy() -> dict[str, Any]:
    return {
        "policy": "grounding-feedback-shared-two-attempts-v1",
        "max_attempts": 2,
        "max_regenerations": 1,
        "scope": "parsed_reconcile_grounding_labels_or_refs_only",
        "eligible_codes": [GROUND_LABELS_PATTERN, GROUND_REFS_PATTERN],
        "request_composition": (
            "unchanged_fixed_context_plus_relation_feedback_code_counts_fixed_rules"
        ),
        "final_validation": "unchanged_strict_grounding_and_partition_before_checkpoint",
        "feedback_fields": ["code", "counts", "rules"],
        "max_count": 1_000_000,
        "raw_response_retransmitted": False,
        "budget_scope": "same_fixed_input_and_unfinished_frontier_across_executions",
        "identity": "source_fingerprint_model_rules_and_logical_content_not_preparation_time",
        "rules": {
            "labels": "Return exactly one grounding for each required merge/split label. "
            "No other labels are permitted. "
            "Merged labels contain multiple Seeds; split labels are the original initial_group. "
            "Do not invent explanations or relations; regenerate from the supplied evidence.",
            "refs": "Use distinct supplied support IDs, at most the number of member Seeds. "
            "Every member must intersect its own support_ids. Re-evaluate the full window; "
            "do not invent evidence or silently remove members.",
        },
    }


def grounding_feedback(code: str) -> dict[str, Any] | None:
    if len(code) > 80:
        return None
    match = re.fullmatch(GROUND_LABELS_PATTERN, code)
    kind = "labels"
    keys: tuple[str, ...] = ("missing", "outside", "duplicate")
    if match is None:
        kind, keys = "refs", ("duplicate", "excess", "outside", "uncovered")
        match = re.fullmatch(GROUND_REFS_PATTERN, code)
    if match is None:
        return None
    counts = dict(zip(keys, (int(v) for v in match.groups()[1:]), strict=True))
    if not any(counts.values()) or max(counts.values()) > 1_000_000:
        return None
    return {"code": code, "counts": counts, "rules": relation_recovery_policy()["rules"][kind]}


def feedback_context(context: dict[str, Any], code: str | None = None) -> dict[str, Any]:
    """None reserves the largest permitted feedback; never send that planning value."""
    if "relation_feedback" in context:
        raise CollectionError("analysis_relation_feedback_invalid", "복구 문맥이 중복됐습니다.")
    if code is None:
        candidates = [
            grounding_feedback(
                "candidate_relation_grounding_merge_labels_m1000000_x1000000_d1000000"
            ),
            grounding_feedback(
                "candidate_relation_grounding_merge_refs_d1000000_e1000000_x1000000_u1000000"
            ),
        ]
        feedback = max(candidates, key=lambda v: len(canonical(v).encode()))
    else:
        feedback = grounding_feedback(code)
    if feedback is None:
        raise CollectionError(
            "analysis_relation_feedback_invalid", "승인되지 않은 복구 조건입니다."
        )
    return {**context, "relation_feedback": feedback}


def relation_work_digest(
    instruction: str, chunks: list[dict[str, Any]], context: dict[str, Any], schema: dict[str, Any]
) -> str:
    # Source fingerprint is bound separately by the parent. Avoid resetting the
    # work budget through a new preparation/read timestamp or source table encoding.
    content = {
        k: v
        for k, v in context.items()
        if k not in {"source_tables", "source_values", "source_encoding", "supports"}
    }
    content["supports"] = [
        {k: v for k, v in support.items() if k != "source_ids"} for support in context["supports"]
    ]
    return sha256(canonical([instruction, chunks, content, schema]).encode())


def compact_sources(context: dict[str, Any]) -> dict[str, Any]:
    """Lossless column/value tables: preserve order, types and every observation.

    Equivalent content has many Collection/timepoint aliases. Repeating the full
    field names and UUIDs for each support dominates relation prompts. Factor only
    that representation; no source field, alias, support text or Seed is omitted.
    """
    tables: list[dict[str, Any]] = []
    columns: dict[tuple[str, ...], int] = {}
    values: list[Any] = []
    value_indices: dict[str, int] = {}
    indices: dict[str, tuple[int, int]] = {}
    supports = []
    for support in context["supports"]:
        refs = []
        for source in support["source"]:
            encoded = canonical(source)
            if encoded not in indices:
                keys = tuple(source)  # Distinguish absent fields from explicit null.
                if keys not in columns:
                    columns[keys] = len(tables)
                    tables.append({"columns": list(keys), "rows": []})
                table_index = columns[keys]
                row = []
                for value in source.values():
                    literal = canonical(value)  # Distinguish bool/int/null/string.
                    if literal not in value_indices:
                        value_indices[literal] = len(values)
                        values.append(value)
                    row.append(value_indices[literal])
                indices[encoded] = (table_index, len(tables[table_index]["rows"]))
                tables[table_index]["rows"].append(row)
            refs.append(indices[encoded])
        supports.append({k: v for k, v in support.items() if k != "source"} | {"source_ids": refs})
    return {
        "seeds": context["seeds"],
        "supports": supports,
        "source_tables": tables,
        "source_values": values,
        "source_encoding": (
            "Each support.source_ids entry is [table_index,row_index] in source_tables. "
            "Zip table.columns with that row; each cell indexes source_values. "
            "All source observations and their original order are retained."
        ),
    }


def pair_cover(
    window: list[int],
    fits: Callable[[list[int]], bool],
) -> list[list[int]]:
    """Retain every pair of an already bounded retrieval window, never global all-pairs.

    Splitting into halves plus one adjacent bridge loses other cross-cut relations.
    Bisect cross rectangles instead; overlapping subsets are deduplicated. A pair
    that cannot fit is a pre-I/O failure, never silently removed.
    """
    result: dict[tuple[int, ...], list[int]] = {}
    positions = {index: order for order, index in enumerate(window)}

    def cover(left: list[int], right: list[int]) -> None:
        combined = sorted(left + right, key=positions.__getitem__)
        if fits(combined):
            result.setdefault(tuple(combined), combined)
            return
        if right:
            if max(len(left), len(right)) <= 1:
                raise CollectionError(
                    "analysis_request_contract_limit_relation_pair",
                    "두 Seed의 필수 근거·출처를 보존한 관계 요청을 구성할 수 없습니다.",
                )
            if len(left) >= len(right):
                middle = len(left) // 2
                cover(left[:middle], right)
                cover(left[middle:], right)
            else:
                middle = len(right) // 2
                cover(left, right[:middle])
                cover(left, right[middle:])
        else:
            if len(left) <= 2:
                raise CollectionError(
                    "analysis_request_contract_limit_relation_pair",
                    "필수 관계 문맥이 로컬 요청 계약 한도를 초과했습니다.",
                )
            middle = len(left) // 2
            cover(left[:middle], [])
            cover(left[middle:], [])
            cover(left[:middle], left[middle:])

    cover(window, [])
    return list(result.values())


class Grounding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=48)
    support_ids: list[str] = Field(min_length=1, max_length=64)


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    assignments: dict[str, int]
    grounds: list[Grounding]
    splits: list[Grounding]  # label denotes the previous initial_group.


def review_schema(ids: list[str], supports: list[str]) -> type[Reconciliation]:
    metadata: dict[str, Any] = {
        "properties": {
            key: {"type": "integer", "minimum": 0, "maximum": len(ids) - 1} for key in ids
        },
        "required": ids,
        "additionalProperties": False,
    }
    support_schema: dict[str, Any] = {"items": {"type": "string", "enum": supports}}
    grounding = create_model(
        "ReviewGrounding",
        __base__=Grounding,
        support_ids=(
            list[str],
            Field(min_length=1, max_length=64, json_schema_extra=support_schema),
        ),
    )
    return create_model(
        "BatchReconciliation",
        __base__=Reconciliation,
        assignments=(dict[str, int], Field(json_schema_extra=metadata)),
        grounds=(list[grounding], ...),  # type: ignore[valid-type]
        splits=(list[grounding], ...),  # type: ignore[valid-type]
    )


def tokens(text: str) -> set[str]:
    words = {w for w in re.findall(r"\w+", text.casefold()) if len(w) >= 3}
    for word in re.findall(r"[가-힣]{2,}", text):
        words.update(word[i : i + 2] for i in range(len(word) - 1))
    return words


def windows(
    seeds: list[dict[str, Any]], source_features: list[set[str]], budget: int
) -> list[list[int]]:
    """Query every Seed against a global inverted index; no Cartesian pair scan.

    Rare semantic/source features rank candidates, never authorize a merge. Lexical
    neighbors and adjacent cores provide alternative views even for common terms.
    Every core is reviewed; retrieval can still miss semantically opaque relations.
    """
    n = len(seeds)
    postings: dict[str, list[int]] = defaultdict(list)
    features = []
    for i, seed in enumerate(seeds):
        keys = tokens(" ".join([seed["title"], seed["rationale"], *seed["search_terms"]]))
        keys |= source_features[i]
        features.append(keys)
        for key in sorted(keys):
            postings[key].append(i)
    ordered = sorted(
        range(n), key=lambda i: (seeds[i]["title"].casefold(), seeds[i]["rationale"], i)
    )
    positions = {index: position for position, index in enumerate(ordered)}
    cores: list[list[int]] = []
    for i, seed in enumerate(seeds):
        if (
            not cores
            or len(cores[-1]) == CORE_SEEDS
            or sum(len(canonical(seeds[j])) for j in cores[-1]) + len(canonical(seed)) > budget // 4
        ):
            cores.append([])
        cores[-1].append(i)
    result = []
    for core in cores:
        selected = list(core)
        suggestions: list[int] = []
        # Bridges are review opportunities only, not positive relation edges.
        suggestions.extend(i for i in (core[0] - 1, core[-1] + 1) if 0 <= i < n)
        ranked: dict[int, list[int]] = {}
        for i in core:
            scores: dict[int, float] = defaultdict(float)
            for key in sorted(features[i]):
                matches = postings[key]
                if len(matches) > max(32, n // 4):
                    continue  # Generic path/technology terms must not induce an all-pairs scan.
                weight = math.log1p(n / len(matches))
                for j in matches:
                    if j not in core:
                        scores[j] += weight
            ranked[i] = sorted(scores, key=lambda j: (-scores[j], j))[:4]
        for depth in range(4):
            suggestions.extend(ranked[i][depth] for i in core if len(ranked[i]) > depth)
            if depth == 0:
                for i in core:
                    p = positions[i]
                    suggestions.extend(ordered[q] for q in (p - 1, p + 1) if 0 <= q < n)
        size = sum(len(canonical(seeds[i])) for i in selected)
        for j in suggestions:
            cost = len(canonical(seeds[j]))
            if j not in selected and len(selected) < WINDOW_SEEDS and size + cost <= budget // 2:
                selected.append(j)
                size += cost
        result.append(selected)
    return result


def partition(ids: Sequence[str], reviews: list[dict[str, Any]]) -> list[list[str]]:
    """Only explicit reviewed co-membership forms an edge. Reject inconsistent chains."""
    parent = {key: key for key in ids}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for review in reviews:
        groups: dict[int, list[str]] = defaultdict(list)
        for key, label in review["assignments"].items():
            groups[label].append(key)
        for group in groups.values():
            for key in group[1:]:
                parent[root(key)] = root(group[0])
    for review in reviews:
        labels: dict[str, int] = {}
        for key, label in review["assignments"].items():
            r = root(key)
            if r in labels and labels[r] != label:
                raise CollectionError(
                    "candidate_relation_conflict", "그룹 관계 검토 결과가 서로 모순됩니다."
                )
            labels[r] = label
    groups_by_root: dict[str, list[str]] = {}
    for key in ids:
        groups_by_root.setdefault(root(key), []).append(key)
    return list(groups_by_root.values())


def validate_review(output: Reconciliation, context: dict[str, Any]) -> None:
    seeds = {s["seed_id"]: s for s in context["seeds"]}
    expected, actual = set(seeds), set(output.assignments)
    bad_labels = sum(not 0 <= v < len(seeds) for v in output.assignments.values())
    if expected != actual or bad_labels:
        raise CollectionError(
            f"candidate_relation_invalid_m{len(expected - actual)}"
            f"_x{len(actual - expected)}_v{bad_labels}",
            "전역 그룹 배정이 유효하지 않습니다.",
        )
    grouped: dict[int, list[str]] = defaultdict(list)
    for key, label in output.assignments.items():
        grouped[label].append(key)
    previous: dict[int, list[str]] = defaultdict(list)
    for key, seed in seeds.items():
        previous[seed["initial_group"]].append(key)
    required_merges = {label for label, members in grouped.items() if len(members) > 1}
    required_splits = {
        label
        for label, members in previous.items()
        if len({output.assignments[key] for key in members}) > 1
    }
    available = {s["id"] for s in context["supports"]}
    # Preserve the rejection rules; only bounded counts cross the worker error channel.
    # Label validation precedes references because invalid labels have no member set.
    for kind, grounds, required, members_by_label in (
        ("merge", output.grounds, required_merges, grouped),
        ("split", output.splits, required_splits, previous),
    ):
        labels = {g.label for g in grounds}
        missing, outside, duplicate = (
            len(required - labels),
            len(labels - required),
            len(grounds) - len(labels),
        )
        if missing or outside or duplicate:
            raise CollectionError(
                f"candidate_relation_grounding_{kind}_labels_m{missing}_x{outside}_d{duplicate}",
                "병합·분리 관계의 검토 근거가 누락·중복됐습니다.",
            )
        blank_reasons = sum(not ground.reason.strip() for ground in grounds)
        if blank_reasons:
            raise CollectionError(
                f"candidate_relation_grounding_{kind}_reason_blank_n{blank_reasons}",
                "병합·분리 관계의 설명이 비어 있습니다.",
            )
        duplicate_refs = excess_refs = outside_refs = uncovered = 0
        for ground in grounds:
            refs = set(ground.support_ids)
            members = members_by_label[ground.label]
            allowed = available.intersection(
                ref for key in members for ref in seeds[key]["support_ids"]
            )
            duplicate_refs += len(ground.support_ids) - len(refs)
            excess_refs += max(0, len(refs) - len(members))
            outside_refs += len(refs - allowed)
            uncovered += sum(not refs.intersection(seeds[key]["support_ids"]) for key in members)
        if duplicate_refs or excess_refs or outside_refs or uncovered:
            raise CollectionError(
                f"candidate_relation_grounding_{kind}_refs"
                f"_d{duplicate_refs}_e{excess_refs}_x{outside_refs}_u{uncovered}",
                "병합·분리 관계가 제공된 근거와 연결되지 않습니다.",
            )
