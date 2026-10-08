"""Browser tests own their database, Git fixtures and application process."""

import argparse
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import uvicorn
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from verification_db import temporary_database

from project_log.db import Database


def fixture(root: Path, partial: bool = False, empty: bool = False) -> None:
    root.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                *args,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init", "-b", "start/here" if empty else "main")
    git("config", "user.name", "Browser Test")
    git("config", "user.email", "browser@example.invalid")
    if empty:
        return
    (root / "README.md").write_text("Test project\n")
    (root / "nested").mkdir()
    (root / "nested/file.txt").write_text("baseline\n")
    git("add", ".")
    git("commit", "-m", "initial")
    if partial:
        (root / "nested").rename(root / "original-nested")
        outside = root.parent / "outside"
        outside.mkdir()
        (root / "nested").symlink_to(outside, target_is_directory=True)
    else:
        (root / "README.md").write_text("Changed after commit\n")
        (root / ".git/info/exclude").write_text("/docs/\n")
        (root / "docs/decisions").mkdir(parents=True)
        (root / "docs/decisions/local.md").write_text("Local development decision\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--slow", action="store_true")
    args = parser.parse_args()
    if args.slow:
        from project_log import worker as worker_module

        original_collect = worker_module.collect

        def slow_collect(*args):
            time.sleep(4)
            return original_collect(*args)

        worker_module.collect = slow_collect

    fixture(args.root / "repo")
    fixture(args.root / "partial-repo", partial=True)
    fixture(args.root / "empty-repo", empty=True)

    # Uvicorn re-raises SIGTERM after shutdown. A Python exit keeps DB cleanup reachable.
    def terminate(_signal: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    with temporary_database() as dsn:
        name = conninfo_to_dict(dsn)["dbname"]
        print(f"Temporary verification database: {name}", flush=True)
        os.environ["PROJECT_LOG_DATABASE_URL"] = dsn
        os.environ["PROJECT_LOG_STORAGE_ROOT"] = str(args.root / "storage")
        if args.legacy:
            # Synthetic pre-feature Project and UNKNOWN snapshot, only in the temporary DB.
            database = Database(dsn)
            database.migrate()
            project, collection = str(uuid.uuid4()), str(uuid.uuid4())
            repository = args.root / "repo"
            with database.connect() as conn:
                conn.execute(
                    "INSERT INTO projects(id,name,path,repository_key,status,"
                    "coding_agent,repository_info) "
                    "VALUES (%s,'기존 프로젝트',%s,%s,'completed','none','{}')",
                    (project, str(repository), str(repository / ".git")),
                )
                conn.execute(
                    "INSERT INTO collections(id,project_id,state,snapshot,policy) "
                    "VALUES (%s,%s,'completed',%s,'{}')",
                    (collection, project, Jsonb({})),
                )
        try:
            uvicorn.run(
                "project_log.api:create_app", factory=True, host="127.0.0.1", port=args.port
            )
        except SystemExit as exc:
            if exc.code != 0:
                raise
    print(f"Temporary verification database removed: {name}", flush=True)
