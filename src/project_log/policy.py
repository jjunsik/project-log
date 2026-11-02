"""Conservative local collection policy; classification is a hint, never an importance gate."""

import base64
import hashlib
from pathlib import PurePosixPath
from typing import Any

MAX_BODY = 1024 * 1024
SNAPSHOT_BYTES = 16 * MAX_BODY
SNAPSHOT_SECONDS = 5.0
DOCUMENT_ROOT = b"docs"
SERVICE_SECRETS_DIRECTORY = ".project-log-secrets"
SYSTEM_PATHSPECS = [
    b":(exclude,icase,glob)**/.project-log-secrets",
    b":(exclude,icase,glob)**/.project-log-secrets/**",
]
# Read-only compatibility with already stored exclusions, never new detection results.
LEGACY_SENSITIVITY_REASONS = frozenset({"sensitive_path", "suspected_secret"})
POLICY = {
    "version": 6,
    "capture_branch": "configured_local_branch_checkout_required",
    "max_body_bytes": MAX_BODY,
    "snapshot_body_bytes": SNAPSHOT_BYTES,
    "snapshot_seconds": SNAPSHOT_SECONDS,
    "snapshot_max_entries": 20000,
    "git_command_seconds": 15,
    "git_output_bytes": 32 * MAX_BODY,
    "history_list_seconds": 60,
    "commit_read_bytes": 2 * MAX_BODY,
    "diff_read_bytes": 4 * MAX_BODY,
    "status_filters": "disabled",
    "untracked": "safe_regular_text",
    "ignored": "metadata_only_no_recursion",
    "development_documents": "recursive_repository_root_docs_with_existing_safety_checks",
    "git_history": "collection_refs_and_head_reachable",
    "historical_source": "original_git_object_required",
    "sensitivity": "user_review_only_no_automatic_detection",
    "system_exclusions": [".git", SERVICE_SECRETS_DIRECTORY],
}


def system_path(path: str | bytes) -> bool:
    """Explicit system storage boundary, independent of content or inferred sensitivity."""
    raw = path if isinstance(path, bytes) else path.encode("utf-8", errors="surrogateescape")
    return bool({b".git", SERVICE_SECRETS_DIRECTORY.encode()} & set(raw.lower().split(b"/")))


def content_reason(raw: bytes) -> str | None:
    if len(raw) > MAX_BODY:
        return "large"
    if b"\0" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8"
    return None


def path_info(raw: bytes) -> dict[str, Any]:
    # byte-exact paths survive non-UTF8 filenames; display text is not the locator.
    return {
        "path": raw.decode("utf-8", errors="replace"),
        "path_b64": base64.b64encode(raw).decode("ascii"),
    }


def kind_hint(path: str) -> str:
    p = PurePosixPath(path.lower())
    if p.name.startswith(("readme", "changelog", "contributing", "license")) or (
        p.name == "agents.md"
        or any(x in {"docs", "doc", "design", "architecture"} for x in p.parts)
    ):
        return "document_candidate"
    if p.name in {"makefile", "dockerfile", "pyproject.toml", "package.json"} or p.suffix in {
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".lock",
    }:
        return "configuration_candidate"
    return "repository_file"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
