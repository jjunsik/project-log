"""Manual transmission review policy and the established provider source projection.

No content inspection, masking, inferred permission, or sensitivity classification.
"""

TRANSFER_POLICY = "user-reviewed-transfer-v1-2026-10-19"
SOURCE_KEYS = (
    "kind",
    "collection_id",
    "commit_oid",
    "parent_oid",
    "oid",
    "path_b64",
    "old_path_b64",
    "new_path_b64",
    "layer",
    "filename",
    "observed_at",
    "body_observed_at",
    "read_at",
    "added_at",
    "provenance",
)
