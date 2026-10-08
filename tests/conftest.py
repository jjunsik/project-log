import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from project_log.db import Database
from scripts.verification_db import temporary_database


@pytest.fixture
def db() -> Iterator[Database]:
    with temporary_database() as dsn:
        database = Database(dsn)
        database.migrate()
        yield database


@pytest.fixture
def git(tmp_path: Path) -> Callable[..., bytes]:
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
            stderr=subprocess.STDOUT,
        ).strip()

    run("init", "-b", "main")
    run("config", "user.name", "Synthetic Developer")
    run("config", "user.email", "test@example.invalid")
    return run
