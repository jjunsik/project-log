"""Local request budgets and provider grammar projection, not Google's compiler.

Domain Pydantic/graph validation remains authoritative and unchanged. The wire
grammar omits branch-expensive array upper bounds; it does not authorize
values outside the original schema or stage-specific checks.
"""

import json
from typing import Any

from project_log.analysis_input import canonical
from project_log.git import CollectionError

REQUEST_POLICY = "bounded-wire-lossless-sources-v1"
TEXT_WIRE_BYTES = 128 * 1024
SCHEMA_BYTES = 32 * 1024  # Includes the existing reviewed 256-key consolidation schema.
SCHEMA_NODES = 1024
SCHEMA_DEPTH = 12
ENUM_VALUES = 512
ENUM_BYTES = 32 * 1024
OMITTED_GRAMMAR_KEYS = frozenset({"title", "maxItems"})
SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "$id",
        "$anchor",
        "$schema",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "anyOf",
        "oneOf",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "default",
        "minLength",
        "maxLength",
        "propertyOrdering",
    }
)


def request_policy() -> dict[str, Any]:
    return {
        "policy": REQUEST_POLICY,
        "text_wire_bytes": TEXT_WIRE_BYTES,
        "schema_bytes": SCHEMA_BYTES,
        "schema_nodes": SCHEMA_NODES,
        "schema_depth": SCHEMA_DEPTH,
        "enum_values": ENUM_VALUES,
        "enum_bytes": ENUM_BYTES,
        "omitted_grammar_keys": sorted(OMITTED_GRAMMAR_KEYS),
        "local_domain_validation": "unchanged_strict_schema_and_stage_checks",
        "relation_sources": "lossless_column_value_tables_all_observations",
        "relation_split": "preserve_every_pair_in_each_original_retrieval_window",
    }


class RequestLimit(CollectionError):
    """Measured local constraint, never a provider error or a consumed attempt."""

    def __init__(self, kind: str, measured: int | None = None, maximum: int | None = None):
        super().__init__(
            "analysis_request_contract_limit_" + kind,
            "로컬 Gemini 요청 계약의 크기·복잡도 한도를 초과했습니다. 완료 결과는 보존됩니다.",
        )
        self.details: dict[str, Any] = {
            "stage": "request_plan",
            "constraint": kind,
            "condition": "measured_gt_limit" if measured is not None else "unknown",
            "cause_status": "CONFIRMED" if measured is not None else "UNKNOWN",
            "measured_value": measured,
            "limit_value": maximum,
        }
        if "bytes" in kind:
            self.details.update(measured_bytes=measured, limit_bytes=maximum)


def limit(kind: str, measured: int | None = None, maximum: int | None = None) -> RequestLimit:
    return RequestLimit(kind, measured, maximum)


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate schema positions only. Never treat user property/enum data as keys."""

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict) or set(node) - SCHEMA_KEYS:
            raise ValueError("Unsupported schema position")
        if "$ref" in node and any(not key.startswith("$") for key in node):
            raise ValueError("Invalid ref sibling")
        for key in ("properties", "$defs"):
            for child in node.get(key, {}).values():
                walk(child)
        for key in ("items", "additionalProperties"):
            if key in node:
                walk(node[key])
        for key in ("anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, []):
                walk(child)

    try:
        json.dumps(schema, allow_nan=False)
        walk(schema)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise CollectionError(
            "analysis_request_schema_invalid", "로컬에서 지원하는 Gemini Schema 계약이 아닙니다."
        ) from None


def provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    validate_schema(schema)
    nodes = enums = enum_bytes = 0

    def walk(node: Any, depth: int, refs: frozenset[str]) -> Any:
        nonlocal nodes, enums, enum_bytes
        if isinstance(node, bool):
            return node
        nodes += 1
        if nodes > SCHEMA_NODES:
            raise limit("schema_nodes", nodes, SCHEMA_NODES)
        if depth > SCHEMA_DEPTH:
            raise limit("schema_depth", depth, SCHEMA_DEPTH)
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in refs:
                raise CollectionError(
                    "analysis_request_schema_invalid", "지원하지 않는 Schema 참조입니다."
                )
            target = schema.get("$defs", {}).get(ref.removeprefix("#/$defs/"))
            if target is None:
                raise CollectionError("analysis_request_schema_invalid", "Schema 참조가 없습니다.")
            # Count resolved depth/branches too; the returned schema retains $ref.
            walk(target, depth, refs | {ref})
        values = node.get("enum", [])
        enums += len(values)
        enum_bytes += len(canonical(values).encode())
        if enums > ENUM_VALUES:
            raise limit("schema_enum", enums, ENUM_VALUES)
        if enum_bytes > ENUM_BYTES:
            raise limit("schema_enum_bytes", enum_bytes, ENUM_BYTES)
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in OMITTED_GRAMMAR_KEYS:
                continue
            if key in ("properties", "$defs"):
                result[key] = {name: walk(child, depth + 1, refs) for name, child in value.items()}
            elif key in ("items", "additionalProperties"):
                result[key] = walk(value, depth + 1, refs)
            elif key in ("anyOf", "oneOf", "prefixItems"):
                result[key] = [walk(child, depth + 1, refs) for child in value]
            else:
                result[key] = value
        return result

    result: dict[str, Any] = walk(schema, 0, frozenset())
    measured = len(canonical(result).encode())
    if measured > SCHEMA_BYTES:
        raise limit("schema_bytes", measured, SCHEMA_BYTES)
    return result


def text_wire_bytes(body: dict[str, Any]) -> int:
    """Conservative SDK-style JSON size; inline bytes have a separate existing cap."""
    contents = [
        {**c, "parts": [p for p in c["parts"] if "inlineData" not in p]} for c in body["contents"]
    ]
    return len(json.dumps({**body, "contents": contents}, allow_nan=False).encode())


def validate_wire(body: dict[str, Any]) -> int:
    measured = text_wire_bytes(body)
    if measured > TEXT_WIRE_BYTES:
        raise limit("text_bytes", measured, TEXT_WIRE_BYTES)
    return measured


def validate_plan(
    task: str,
    instruction: str,
    evidence: list[dict[str, Any]],
    context: object,
    schema: dict[str, Any],
) -> int:
    wire_schema = provider_schema(schema)
    text = canonical({"task": task, "context": canonical(context), "evidence": evidence})
    return validate_wire(
        {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": {"parts": [{"text": instruction}]},
            "safetySettings": [],
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
                "responseJsonSchema": wire_schema,
            },
        }
    )
