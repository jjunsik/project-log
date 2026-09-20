"""Immutable JSON records; one local process, plus an OS lock for accidental duplicates."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("Invalid identifier")
    return value


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock, (self.root / ".lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def directory(self, case_id: str, kind: str) -> Path:
        path = self.root / identifier(case_id) / identifier(kind)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path outside pilot store")
        return resolved

    def append(self, case_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"id": uuid.uuid4().hex, "recorded_at": utc_now(), **payload}
        body = {"record": record, "sha256": digest(record)}
        folder = self.directory(case_id, kind)
        folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=folder)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(body, file, ensure_ascii=False, indent=2, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            # UUID records never replace earlier revisions or approvals.
            destination = folder / f"{record['id']}.json"
            os.link(temporary, destination)
            directory_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return record

    def read(self, case_id: str, kind: str, record_id: str) -> dict[str, Any]:
        file = self.directory(case_id, kind) / f"{identifier(record_id)}.json"
        if not file.resolve().is_relative_to(self.root):
            raise ValueError("Path outside pilot store")
        body = json.loads(file.read_text())
        if digest(body["record"]) != body["sha256"]:
            raise ValueError("Stored record integrity check failed")
        record: dict[str, Any] = body["record"]
        return record

    def list(self, case_id: str, kind: str) -> list[dict[str, Any]]:
        folder = self.directory(case_id, kind)
        return sorted(
            [self.read(case_id, kind, file.stem) for file in folder.glob("*.json")],
            key=lambda item: (item["recorded_at"], item["id"]),
        )
