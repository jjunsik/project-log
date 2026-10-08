"""Exercise product API/worker against an unchanged real Git repository, in a fresh DB."""

import argparse
import base64
import hashlib
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from verification_db import temporary_database

from project_log.api import create_app
from project_log.db import Database
from project_log.git import Git, read_working


def file_fingerprint(root: Path, raw: bytes) -> dict[str, object]:
    """Hash in-root bytes without following leaf or parent symlinks."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = raw.split(b"/")
        if any(p in {b"", b".", b".."} for p in parts):
            raise ValueError("Unsafe Git path")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        mode = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
        digest = hashlib.sha256()
        if stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink(parts[-1], dir_fd=fd)))
        elif stat.S_ISREG(mode):
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("File changed during fingerprint")
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            return {"mode": mode, "kind": "non_regular"}
        return {"mode": mode, "sha256": digest.hexdigest()}
    except FileNotFoundError:
        return {"kind": "missing"}
    finally:
        os.close(fd)


def fingerprint(root: Path) -> dict[str, object]:
    git = Git(root)
    entries = git.run("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    files = {}
    for raw in sorted(set(entries.split(b"\0")) - {b""}):
        files[raw.hex()] = file_fingerprint(root, raw)
    index_path = Path(os.fsdecode(git.run("rev-parse", "--git-path", "index").strip()))
    if not index_path.is_absolute():
        index_path = root / index_path
    return {
        "head": git.head(),
        "branch_ref_b64": base64.b64encode(git.symbolic_head()).decode(),
        "refs": git.refs(),
        "status": hashlib.sha256(git.status()).hexdigest(),
        "files": files,
        "index": hashlib.sha256(index_path.read_bytes()).hexdigest()
        if index_path.exists()
        else None,
    }


def verify(root: Path, report: Path, base_branch: str) -> None:
    before = fingerprint(root)
    with temporary_database() as dsn:
        connection = conninfo_to_dict(dsn)
        db = Database(dsn)
        with TestClient(create_app(db)) as client:
            response = client.post(
                "/api/projects",
                json={
                    "path": str(root),
                    "name": root.name,
                    "status": "ongoing",
                    "base_branch": base_branch,
                },
            )
            response.raise_for_status()
            project = response.json()
            cid = project["collections"][0]["id"]
            deadline = time.monotonic() + 120
            while True:
                result = client.get(f"/api/collections/{cid}").json()
                if result["state"] in {"completed", "partial", "failed"}:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("Real repository collection timed out")
                time.sleep(0.1)
            git = Git(root)
            commits = db.all("SELECT * FROM commit_records WHERE collection_id=%s", (cid,))
            collected = {r["oid"] for r in commits}
            expected = set(
                git.run("rev-list", "--all", *(["HEAD"] if git.head() else []))
                .decode()
                .splitlines()
            )
            files = db.all("SELECT metadata FROM head_records WHERE collection_id=%s", (cid,))
            checks = {
                "commit_metadata": True,
                "changes": True,
                "diff_bytes": True,
                "head_tree": True,
                "working_bytes_and_hashes": True,
                "source_blobs": True,
            }
            checked_changes = checked_diffs = checked_bodies = 0
            for commit in commits:
                raw = git.run("cat-file", "commit", commit["oid"])
                headers = raw.partition(b"\n\n")[0].splitlines()
                tree = next(h[5:].decode() for h in headers if h.startswith(b"tree "))
                parents = [h[7:].decode() for h in headers if h.startswith(b"parent ")]
                checks["commit_metadata"] &= commit["metadata"]["tree"] == tree
                checks["commit_metadata"] &= commit["metadata"]["parents"] == parents
                checks["commit_metadata"] &= commit["raw"] is None or bytes(commit["raw"]) == raw
                for parent in parents or [None]:
                    revisions = [parent, commit["oid"]] if parent else ["--root", commit["oid"]]
                    tokens = iter(
                        git.run(
                            "diff-tree",
                            "--no-commit-id",
                            "--name-status",
                            "-r",
                            "-z",
                            "-M",
                            "--no-ext-diff",
                            "--no-textconv",
                            *revisions,
                        ).split(b"\0")
                    )
                    expected_changes = []
                    for status in tokens:
                        if not status:
                            continue
                        old = next(tokens)
                        new = next(tokens) if status[:1] in {b"R", b"C"} else old
                        expected_changes.append((status.decode(), old, new))
                    rows = db.all(
                        """SELECT * FROM change_records WHERE collection_id=%s AND commit_oid=%s
                                    AND parent_oid IS NOT DISTINCT FROM %s""",
                        (cid, commit["oid"], parent),
                    )
                    actual_changes = [
                        (
                            r["metadata"]["status"],
                            base64.b64decode(r["metadata"]["old"]["path_b64"]),
                            base64.b64decode(r["metadata"]["new"]["path_b64"]),
                        )
                        for r in rows
                    ]
                    checks["changes"] &= sorted(actual_changes) == sorted(expected_changes)
                    checked_changes += len(rows)
                    for row in rows:
                        if row["diff"] is None:
                            continue
                        paths = [
                            base64.b64decode(row["metadata"][side]["path_b64"])
                            for side in ("old", "new")
                        ]
                        pathspecs = [b":(literal)" + p for p in paths] + [
                            b":(exclude,literal)" + p + b"/" for p in paths
                        ]
                        expected_diff = git.run(
                            "diff-tree",
                            "--no-commit-id",
                            "--patch",
                            "--full-index",
                            "-r",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--no-color",
                            "--no-renames",
                            *revisions,
                            "--",
                            *pathspecs,
                        )
                        checks["diff_bytes"] &= bytes(row["diff"]) == expected_diff
                        checked_diffs += 1
            head = result["snapshot"]["head"]
            expected_files = {f["path_b64"]: f for f in git.tree(head)} if head else {}
            checks["head_tree"] &= set(expected_files) == {r["metadata"]["path_b64"] for r in files}
            for row in files:
                file = row["metadata"]
                checks["head_tree"] &= all(
                    file[k] == v for k, v in expected_files[file["path_b64"]].items()
                )
            source_samples = []
            for path in (
                "README.md",
                "poc/phase0/src/project_log/service.py",
                "poc/phase0/pyproject.toml",
            ):
                file = next((r["metadata"] for r in files if r["metadata"]["path"] == path), None)
                if not file:
                    continue
                source = client.get(
                    f"/api/collections/{cid}/source",
                    params={"commit": head, "path_b64": file["path_b64"]},
                )
                source.raise_for_status()
                value = source.json()
                matches = value["body"] == git.run("cat-file", "blob", file["oid"]).decode()
                checks["source_blobs"] &= (
                    matches and value["collection_id"] == cid and bool(value["read_at"])
                )
                source_samples.append({"path": path, "oid": file["oid"], "matches": matches})
            working = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
            layers: dict[str, int] = {}
            for row in working:
                layers[row["layer"]] = layers.get(row["layer"], 0) + 1
                if row["body"] is None:
                    continue
                body = bytes(row["body"])
                meta = row["metadata"]
                actual = (
                    git.run("cat-file", "blob", meta["oid"])
                    if row["layer"] == "index"
                    else read_working(root, base64.b64decode(meta["path_b64"]))[0]
                )
                checks["working_bytes_and_hashes"] &= (
                    body == actual and hashlib.sha256(body).hexdigest() == row["sha256"]
                )
                checked_bodies += 1
            checks["snapshot_status"] = result["snapshot"]["status_sha256"] == before["status"]
            checks["snapshot_index_listing"] = (
                result["snapshot"]["index_listing_sha256"]
                == hashlib.sha256(git.index()).hexdigest()
            )
            table_counts = {
                table: db.one(f"SELECT count(*) AS n FROM {table}")["n"]
                for table in (
                    "projects",
                    "collections",
                    "commits",
                    "changes",
                    "head_files",
                    "working_entries",
                    "collection_issues",
                    "schema_migrations",
                )
            }
    after = fingerprint(root)
    data = {
        "verified_at": datetime.now(UTC).isoformat(),
        "database": connection["dbname"],
        "database_endpoint": f"{connection['host']}:{connection['port']}",
        "temporary_database_dropped": True,
        "table_counts": table_counts,
        "repository": str(root),
        "collection": result,
        "expected_commits": sorted(expected),
        "collected_commits": sorted(collected),
        "all_reachable_history_matches": expected == collected,
        "checks": checks,
        "checked_changes": checked_changes,
        "checked_diffs": checked_diffs,
        "checked_working_bodies": checked_bodies,
        "working_layers": layers,
        "source_samples": source_samples,
        "git_index_refs_and_nonignored_files_unchanged": before == after,
        "before": before,
        "after": after,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in data.items() if k not in {"before", "after", "collection"}},
            ensure_ascii=False,
        )
    )
    print(json.dumps(result["summary"], ensure_ascii=False))
    assert result["state"] == "completed", result["issues"]
    assert expected == collected and all(checks.values()) and source_samples and before == after


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-branch", required=True)
    args = parser.parse_args()
    verify(args.repository.resolve(), args.report.resolve(), args.base_branch)
