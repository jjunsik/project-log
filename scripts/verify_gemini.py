"""Explicit operator-only live check. Does not connect to PostgreSQL or read project data."""

import argparse
import json

from project_log.gemini import (
    NOTICE,
    POLICY_VERSION,
    GeminiClient,
    GeminiConfig,
    SyntheticConsent,
    synthetic_digest,
)
from project_log.git import CollectionError


def main() -> int:
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument("--confirm-free-tier", action="store_true")
    parser.add_argument("--confirm-synthetic-transfer", action="store_true")
    args = parser.parse_args()
    if not args.confirm_synthetic_transfer:
        print(NOTICE)
        print("외부 호출하지 않음. 동의 후 --confirm-synthetic-transfer를 지정하세요.")
        return 2
    try:
        config = GeminiConfig.from_environment(free_tier_confirmed=args.confirm_free_tier)
        consent = SyntheticConsent(config.model, synthetic_digest(), POLICY_VERSION, True)
        result = GeminiClient(config).verify_synthetic(consent)
        print(
            json.dumps(
                {
                    "status": "synthetic_integration_verified",
                    "model": config.model,
                    "policy_version": POLICY_VERSION,
                    "input_sha256": consent.input_sha256,
                    "evidence_ids": result.evidence_ids,
                    "usage": json.loads(result.usage_json),
                    "project_data_transferred": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except CollectionError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "message": exc.message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
