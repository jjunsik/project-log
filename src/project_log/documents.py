"""Observe repository-root docs only, without following symlinks."""

import os
import stat
import time
from pathlib import Path
from typing import Any

from project_log.policy import DOCUMENT_ROOT, SNAPSHOT_SECONDS, path_info, sensitive_path, sha256


def scan_documents(
    root: Path, submodules: set[bytes], start: float
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    signature: list[bytes] = []
    issues: list[dict[str, Any]] = []
    visited = 0
    document_root = DOCUMENT_ROOT
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def record(path: bytes, document_root: bytes, reason: str, meta: dict[str, Any]) -> None:
        entry = {
            **path_info(path),
            **meta,
            "document_root": path_info(document_root),
            "document_reason": reason,
        }
        entries.append(entry)
        if reason in {"unreadable_or_symlink_parent", "changed_during_capture"}:
            issues.append(entry)

    try:
        stack = [document_root]
        while stack:
            path = stack.pop()
            if visited >= 20000 or time.monotonic() - start >= SNAPSHOT_SECONDS:
                reason = "snapshot_entry_limit" if visited >= 20000 else "snapshot_budget"
                entry = {**path_info(path), "document_root": path_info(document_root)}
                issues.append({**entry, "document_reason": reason})
                return entries, sha256(b"\0".join(signature)), issues
            visited += 1
            if any(path == s or path.startswith(s + b"/") for s in submodules):
                record(path, document_root, "submodule", {})
                signature.append(path + b":submodule")
                continue
            fd = os.dup(root_fd)
            try:
                parts = path.split(b"/")
                if any(part in {b"", b".", b".."} for part in parts):
                    raise ValueError("Unsafe document path")
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                meta = {
                    "mode": info.st_mode,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
                signature.append(
                    path
                    + b":"
                    + repr((info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns)).encode()
                )
                if stat.S_ISLNK(info.st_mode):
                    record(path, document_root, "symlink", meta)
                elif sensitive_path(os.fsdecode(path)):
                    record(path, document_root, "sensitive_path", meta)
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    os.close(fd)
                    fd = child
                    opened = os.fstat(fd)
                    if opened.st_ino != info.st_ino:
                        record(path, document_root, "changed_during_capture", meta)
                        continue
                    # Bound discovery as well as bodies; avoid unbounded enumeration.
                    children: list[bytes] = []
                    with os.scandir(fd) as listing:
                        for item in listing:
                            if visited + len(stack) + len(children) >= 20000:
                                issues.append(
                                    {
                                        **path_info(path),
                                        "document_root": path_info(document_root),
                                        "document_reason": "snapshot_entry_limit",
                                    }
                                )
                                return entries, sha256(b"\0".join(signature)), issues
                            if time.monotonic() - start >= SNAPSHOT_SECONDS:
                                issues.append(
                                    {
                                        **path_info(path),
                                        "document_root": path_info(document_root),
                                        "document_reason": "snapshot_budget",
                                    }
                                )
                                return entries, sha256(b"\0".join(signature)), issues
                            children.append(path + b"/" + os.fsencode(item.name))
                    stack.extend(sorted(children, reverse=True))
                elif stat.S_ISREG(info.st_mode):
                    entries.append(
                        {
                            **path_info(path),
                            **meta,
                            "document_root": path_info(document_root),
                        }
                    )
                else:
                    record(path, document_root, "non_regular", meta)
            except FileNotFoundError:
                signature.append(path + b":missing")
                if path != document_root:
                    record(path, document_root, "changed_during_capture", {})
            except OSError:
                signature.append(path + b":unreadable")
                record(path, document_root, "unreadable_or_symlink_parent", {})
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)
    return entries, sha256(b"\0".join(signature)), issues
