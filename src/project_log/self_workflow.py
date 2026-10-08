"""Narrow source-checkout exception for Project Log's own development workflow."""

import base64
from pathlib import Path
from typing import Any

SELF_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOTS = (b"current_status.md", b"chatgpt")


def is_self_repository(root: Path) -> bool:
    return root.resolve() == SELF_REPOSITORY_ROOT.resolve()


def pathspecs(root: Path) -> list[bytes]:
    return (
        # A literal Git pathspec also excludes directory descendants. Match this file exactly.
        [b":(top,exclude,glob)current_status.m[d]", b":(top,exclude,literal)chatgpt"]
        if is_self_repository(root)
        else []
    )


def workflow_path(path: bytes) -> bool:
    return (
        path == WORKFLOW_ROOTS[0]
        or path.rstrip(b"/") == WORKFLOW_ROOTS[1]
        or path.startswith(WORKFLOW_ROOTS[1] + b"/")
    )


def metadata_paths(value: Any) -> set[bytes]:
    """Read only actual path identity fields, never body/message mentions."""
    paths: set[bytes] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path_b64", "old_path_b64", "new_path_b64"} and isinstance(item, str):
                paths.add(base64.b64decode(item, validate=True))
            elif key == "path" and isinstance(item, str):
                paths.add(item.encode("utf-8"))
            elif isinstance(item, (dict, list)):
                paths.update(metadata_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.update(metadata_paths(item))
    return paths
