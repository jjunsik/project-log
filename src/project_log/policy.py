"""Conservative local collection policy; classification is a hint, never an importance gate."""

import base64
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

MAX_BODY = 1024 * 1024
SNAPSHOT_BYTES = 16 * MAX_BODY
SNAPSHOT_SECONDS = 5.0
POLICY = {
    "version": 5,
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
    "git_history": "collection_refs_and_head_reachable",
    "historical_source": "original_git_object_required",
}

SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    rb"|(?:ghp_|github_pat_|sk-proj-|sk-ant-)[A-Za-z0-9_-]{16,}"
    rb"|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}"
    rb"|(?i:bearer\s+[a-z0-9._-]{16,})"
    rb"|(?i:(?:password|passwd|api[_-]?key|access[_-]?token|secret|client_secret)"
    rb"\s*[\"']?\s*[:=]\s*[\"']?[a-z0-9_+/=-]{8,})"
    rb"|(?i:[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@)"
)


def sensitive_path(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    return any(
        part in {".git", ".ssh", ".aws", ".gnupg", ".kube", ".npmrc", ".pypirc", ".netrc"}
        or part == ".env"
        or part.startswith(".env.")
        or part.startswith(("id_rsa", "id_ed25519", "credentials", "secrets"))
        or part.endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
        for part in parts
    )


def content_reason(raw: bytes) -> str | None:
    if len(raw) > MAX_BODY:
        return "large"
    if b"\0" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8"
    if SECRET.search(raw):
        return "suspected_secret"
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
