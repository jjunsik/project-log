"""Accepted snapshot codec; no source reads or provider access on load."""

import base64
import json

from project_log.analysis_input import canonical
from project_log.db import Row
from project_log.experience_input import EXPERIENCE_INPUT_POLICY, Evidence, ExperienceInput, Media
from project_log.git import CollectionError


def encode_snapshot(snapshot: ExperienceInput) -> Row:
    return {
        "project_id": snapshot.project_id,
        "prepared_at": snapshot.prepared_at,
        "collections": json.loads(snapshot.collections_json),
        "policy": snapshot.policy,
        **(
            {"privacy_review": json.loads(snapshot.privacy_review_json)}
            if snapshot.policy != EXPERIENCE_INPUT_POLICY
            else {}
        ),
        "entries": [
            {
                "reference": e.reference,
                "source": json.loads(e.source_json),
                "text": e.text,
                "reason": e.reason,
                "limitations": list(e.limitations),
                "media": [
                    {**m.descriptor(), "base64": base64.b64encode(m.raw).decode("ascii")}
                    for m in e.media
                ],
            }
            for e in snapshot.entries
        ],
    }


def decode_snapshot(payload: Row, manifest: Row, digest: str) -> ExperienceInput:
    try:
        entries = tuple(
            Evidence(
                e["reference"],
                canonical(e["source"]),
                e["text"],
                tuple(
                    Media(
                        m["name"],
                        m["mime_type"],
                        base64.b64decode(m["base64"], validate=True),
                        canonical(m["provenance"]),
                        m["use_for_analysis"],
                    )
                    for m in e["media"]
                ),
                e["reason"],
                tuple(e["limitations"]),
            )
            for e in payload["entries"]
        )
        snapshot = ExperienceInput(
            payload["project_id"],
            payload["prepared_at"],
            canonical(payload["collections"]),
            entries,
            payload["policy"],
            canonical(payload.get("privacy_review", {})),
        )
        if canonical(snapshot.manifest()) != canonical(manifest) or snapshot.digest != digest:
            raise ValueError("Snapshot mismatch")
        return snapshot
    except (KeyError, TypeError, ValueError):
        raise CollectionError(
            "analysis_input_corrupt", "고정 분석 입력의 무결성을 확인할 수 없습니다."
        ) from None
