import base64
import hashlib
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.collector import collect
from project_log.db import Database
from project_log.git import CollectionError, Git, read_working
from project_log.service import Projects, Registration, Settings
from project_log.worker import Worker


def register(db: Database, root: Path, **kwargs: str) -> tuple[Projects, str]:
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="Test", status="ongoing", base_branch="main", **kwargs)
    )
    return projects, str(p["collections"][0]["id"])


def commit(git: Callable[..., bytes], root: Path, name: str, body: str, message: str) -> str:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body)
    git("add", "--", name)
    git("commit", "-m", message)
    return git("rev-parse", "HEAD").decode()


def test_empty_repository_duplicate_and_project_settings(db, git, tmp_path):
    root = tmp_path / "repo"
    projects, cid = register(db, root)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "completed"
    assert row["snapshot"]["head"] is None
    assert row["summary"]["commits"] == 0
    with pytest.raises(CollectionError, match="이미 등록"):
        register(db, root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(CollectionError, match="이미 등록"):
        register(db, alias)
    pid = str(row["project_id"])
    projects.update(pid, Settings(name="Changed", status="completed", base_branch="main"))
    assert (
        projects.update(pid, Settings(name="Changed", status="ongoing", base_branch="main"))[
            "status"
        ]
        == "ongoing"
    )


def test_project_api_without_coding_agent_preserves_legacy_storage(db, git, tmp_path):
    root = tmp_path / "repo"
    with TestClient(create_app(db, start_worker=False)) as client:
        response = client.post(
            "/api/projects",
            json={"path": str(root), "name": "No agent", "status": "new", "base_branch": "main"},
        )
        assert response.status_code == 201
        project = response.json()
        pid = project["id"]
        assert "coding_agent" not in project
        stored = db.one("SELECT * FROM projects WHERE id=%s", (pid,))
        assert stored["coding_agent"] == "none"
        # Existing user values remain historical storage, independent of settings updates.
        db.execute("UPDATE projects SET coding_agent='codex' WHERE id=%s", (pid,))
        assert "coding_agent" not in client.get("/api/projects").json()[0]
        assert "coding_agent" not in client.get(f"/api/projects/{pid}").json()
        updated = client.patch(
            f"/api/projects/{pid}",
            json={"name": "Renamed", "status": "completed", "base_branch": "main"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Renamed"
        assert updated.json()["status"] == "completed"
        assert "coding_agent" not in updated.json()
        after = db.one("SELECT * FROM projects WHERE id=%s", (pid,))
        assert after["coding_agent"] == "codex"
        for key in ("id", "created_at", "path", "repository_key", "repository_info"):
            assert after[key] == stored[key]
        assert after["updated_at"] >= stored["updated_at"]
        assert (
            client.patch(
                f"/api/projects/{pid}",
                json={
                    "name": "Renamed",
                    "status": "completed",
                    "base_branch": "main",
                    "coding_agent": "none",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/projects",
                json={
                    "path": str(root),
                    "name": "No agent",
                    "status": "new",
                    "base_branch": "main",
                    "coding_agent": "none",
                },
            ).status_code
            == 422
        )
        assert db.one("SELECT count(*) AS n FROM projects")["n"] == 1
        assert (
            db.one("SELECT coding_agent FROM projects WHERE id=%s", (pid,))["coding_agent"]
            == "codex"
        )


def test_invalid_paths_and_api_validation(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "child").mkdir()
    with TestClient(create_app(db, start_worker=False)) as client:
        for path in [tmp_path / "missing", tmp_path, root / "child"]:
            response = client.post(
                "/api/projects",
                json={"path": str(path), "name": "X", "status": "new", "base_branch": "main"},
            )
            assert response.status_code == 422
        assert (
            client.post(
                "/api/projects",
                json={"path": str(root), "name": "  ", "status": "new", "base_branch": "main"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/projects",
                json={
                    "path": str(root),
                    "name": "X",
                    "status": "new",
                    "base_branch": "main",
                    "description": "not allowed",
                },
            ).status_code
            == 422
        )
        assert (
            client.get("/api/projects", headers={"origin": "https://evil.invalid"}).status_code
            == 403
        )
        assert client.get("/api/projects", headers={"host": "evil.invalid"}).status_code == 400
        assert (
            client.post(
                "/api/projects", content="x", headers={"content-type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            client.post(
                "/api/projects",
                content='"' + "a" * 20000 + '"',
                headers={"content-type": "application/json"},
            ).status_code
            == 413
        )
        assert client.get("/api/projects/not-a-uuid").status_code == 422
        assert client.get("/api/health").json()["database"] == "ready"
    assert db.one("SELECT count(*) AS n FROM projects")["n"] == 0


def test_history_all_branches_merge_rename_delete_and_source(db, git, tmp_path):
    root = tmp_path / "repo"
    first = commit(git, root, "README.rst", "Initial document\n", "first")
    git("checkout", "-b", "side")
    side = commit(git, root, "settings.custom", "port = 8123\n", "side setting")
    git("checkout", "main")
    git("mv", "README.rst", "guide.rst")
    git("commit", "-m", "rename")
    git("merge", "--no-ff", "side", "-m", "merge")
    merge = git("rev-parse", "HEAD").decode()
    git("rm", "guide.rst")
    git("commit", "-m", "delete")
    git("tag", "-a", "v1", "-m", "tag")
    git("branch", "old", first)
    before_status = Git(root).status()
    before_refs = Git(root).refs()
    projects, cid = register(db, root)
    # Later commits must not silently enter a registration-time collection.
    late = commit(git, root, "late.txt", "later\n", "not in snapshot")
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "completed", row["issues"]
    oids = {
        r["oid"] for r in db.all("SELECT oid FROM commit_records WHERE collection_id=%s", (cid,))
    }
    assert len(oids) == 5 and {first, side, merge} <= oids and late not in oids
    parents = db.one(
        "SELECT metadata FROM commit_records WHERE collection_id=%s AND oid=%s", (cid, merge)
    )["metadata"]["parents"]
    assert len(parents) == 2
    changes = db.all("SELECT * FROM change_records WHERE collection_id=%s", (cid,))
    assert any(r["metadata"]["status"].startswith("R") for r in changes)
    assert any(r["metadata"]["status"] == "D" for r in changes)
    assert {r["parent_oid"] for r in changes if r["commit_oid"] == merge} == set(parents)
    source = projects.source(cid, first, base64.b64encode(b"README.rst").decode())
    assert source["body"] == "Initial document\n"
    assert (
        before_status == b"" and before_refs
    )  # Observation only; target was not mutated by collector.
    assert Git(root).head() == late
    assert Git(root).status() == b""


def test_staged_unstaged_untracked_ignored_and_no_target_writes(db, git, tmp_path):
    root = tmp_path / "repo"
    first = commit(git, root, "main.py", "head\n", "initial")
    (root / "main.py").write_text("staged\n")
    git("add", "main.py")
    (root / "main.py").write_text("unstaged\n")
    (root / "untracked.md").write_text("safe untracked retained\n")
    (root / ".gitignore").write_text(".env\nignored/\n")
    (root / ".env").write_text("PASSWORD=synthetic-credential-only\n")
    (root / "ignored").mkdir()
    (root / "ignored/private.txt").write_text("private\n")
    index_before = (root / ".git/index").read_bytes()
    status_before = Git(root).status()
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert next(bytes(r["body"]) for r in rows if r["layer"] == "index") == b"staged\n"
    assert next(bytes(r["body"]) for r in rows if r["layer"] == "working") == b"unstaged\n"
    assert all(r["body"] is None for r in rows if r["layer"] == "ignored")
    untracked = next(r for r in rows if r["metadata"]["path"] == "untracked.md")
    assert bytes(untracked["body"]) == b"safe untracked retained\n"
    assert untracked["sha256"] == hashlib.sha256(b"safe untracked retained\n").hexdigest()
    assert not any(r["metadata"]["path"].endswith("private.txt") for r in rows)
    assert (root / ".git/index").read_bytes() == index_before
    assert Git(root).head() == first and Git(root).status() == status_before
    assert (root / ".env").read_text() == "PASSWORD=synthetic-credential-only\n"


def test_secrets_binary_large_symlinks_and_unusual_paths(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / ".env").write_text("hidden\n")
    (root / "token.txt").write_text("api_key = syntheticvalue1234567890\n")
    (root / "binary.bin").write_bytes(b"a\0b")
    (root / "large.txt").write_bytes(b"a" * (1024 * 1024 + 1))
    (root / "odd\n한글.md").write_text("safe content\n")
    (root / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_text("outside secret\n")
    git("add", ".")
    git("commit", "-m", "assets")
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    assert db.detail(cid)["state"] == "completed"
    reasons = {
        r["body_reason"]
        for r in db.all("SELECT body_reason FROM change_records WHERE collection_id=%s", (cid,))
    }
    assert {"sensitive_path", "suspected_secret", "binary", "large", "symlink"} <= reasons
    diffs = b"".join(
        bytes(r["diff"]) for r in db.all("SELECT diff FROM change_records WHERE diff IS NOT NULL")
    )
    assert b"syntheticvalue" not in diffs and b"safe content" in diffs
    assert read_working(root, b"link")[1] == "symlink"
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    assert read_working(root, b"parent/outside")[0] is None
    assert read_working(root, b"../outside")[1] == "unsafe_path"


def test_git_does_not_execute_external_helpers(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "a.txt", "hello\n", "first")
    marker = tmp_path / "executed"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    helper.chmod(0o755)
    git("config", "core.fsmonitor", str(helper))
    git("config", "diff.external", str(helper))
    git("config", "diff.danger.textconv", str(helper))
    (root / ".gitattributes").write_text("*.txt diff=danger\n")
    git("add", ".gitattributes")
    # Fixture git operations override fsmonitor so only collection is tested below.
    marker.unlink(missing_ok=True)
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    assert not marker.exists()


def test_worker_failure_removes_attempt_and_manual_uses_current_bytes(
    db, git, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    commit(git, root, "a", "base\n", "first")
    (root / "a").write_text("original dirty\n")
    projects, cid = register(db, root)
    pid = str(db.detail(cid)["project_id"])
    original = Git.run

    def fail(self, *args, **kwargs):
        if args[0] == "rev-list":
            raise CollectionError("simulated_read_failure", "Synthetic failure")
        return original(self, *args, **kwargs)

    worker = Worker(db)
    try:
        with monkeypatch.context() as m:
            m.setattr(Git, "run", fail)
            assert worker.once()
        assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))
        (root / "a").write_text("later dirty\n")
        new = projects.collect_now(pid)
        assert worker.once()
        assert db.detail(str(new["id"]))["state"] == "completed"
        body = db.one(
            "SELECT body FROM working_records WHERE collection_id=%s AND layer='working'",
            (new["id"],),
        )["body"]
        assert bytes(body) == b"later dirty\n"
    finally:
        worker.close()


def test_restart_cleans_interrupted_jobs_and_process_lock(db, git, tmp_path, monkeypatch):
    _, cid = register(db, tmp_path / "repo")
    db.execute("UPDATE collections SET state='running' WHERE id=%s", (cid,))
    worker = Worker(db)
    monkeypatch.setattr(worker.thread, "start", lambda: None)
    try:
        with pytest.raises(RuntimeError, match="Another"):
            Worker(db)
        worker.start()
        assert db.detail(cid)["state"] == "cleanup_pending"
        worker.once()
        assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))
    finally:
        worker.close()


def test_background_api_flow_and_persisted_restart(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "README", "read me\n", "initial")
    with TestClient(create_app(db)) as client:
        result = client.post(
            "/api/projects",
            json={
                "name": "Product",
                "path": str(root),
                "status": "ongoing",
                "base_branch": "main",
            },
        )
        assert result.status_code == 201
        project = result.json()
        cid = project["collections"][0]["id"]
        for _ in range(100):
            row = client.get(f"/api/collections/{cid}").json()
            if row["state"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.03)
        assert row["state"] == "completed", row
        assert row["summary"]["commits"] == 1
        records = client.get(f"/api/collections/{cid}/records/head_files").json()
        assert records[0]["metadata"]["kind_hint"] == "document_candidate"
        source = client.get(
            f"/api/collections/{cid}/source",
            params={
                "commit": row["snapshot"]["head"],
                "path_b64": records[0]["metadata"]["path_b64"],
            },
        ).json()
        assert source["body"] == "read me\n"
        assert client.get(f"/api/collections/{cid}/records/unknown").status_code == 404
    with TestClient(create_app(db)) as client:
        assert client.get(f"/api/projects/{project['id']}").json()["name"] == "Product"
        assert client.get(f"/api/collections/{cid}").json()["state"] == "completed"


def test_body_budget_marks_cleanup_with_diagnostic_reason(db, git, tmp_path, monkeypatch):
    import project_log.collector as collector

    root = tmp_path / "repo"
    commit(git, root, "a", "initial\n", "first")
    (root / "a").write_text("modified\n")
    monkeypatch.setattr(collector, "SNAPSHOT_BYTES", 1)
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "cleanup_pending"
    assert any(i["code"] == "snapshot_budget" for i in row["issues"])


def test_non_utf8_path_bytes_are_retained(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "main.txt", "normal\n", "main")
    raw_path = b"odd-\xff.txt"

    # macOS cannot create this filename; Git trees can retain it without checkout.
    def plumbing(*args: str, body: bytes = b"") -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args], input=body).strip()

    blob = plumbing("hash-object", "-w", "--stdin", body=b"safe\n")
    tree = plumbing("mktree", "-z", body=b"100644 blob " + blob + b"\t" + raw_path + b"\0")
    oid = plumbing("commit-tree", tree.decode(), "-m", "byte filename")
    git("update-ref", "refs/heads/odd-filename", oid.decode())
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    files = db.all(
        "SELECT metadata FROM change_records WHERE collection_id=%s AND commit_oid=%s",
        (cid, oid.decode()),
    )
    assert base64.b64decode(files[0]["metadata"]["new"]["path_b64"]) == raw_path
    source = Projects(db).source(cid, oid.decode(), base64.b64encode(raw_path).decode())
    assert source["body"] == "safe\n"
    assert db.detail(cid)["state"] == "completed"


def test_migration_idempotence_and_project_evidence_boundaries(db, git, tmp_path):
    db.migrate()
    root = tmp_path / "repo"
    commit(git, root, "file", "first\n", "first")
    projects, cid = register(db, root)
    collect(db, cid, lambda: False)
    foreign = "f" * 40
    with pytest.raises(CollectionError, match="없습니다"):
        projects.source(cid, foreign, base64.b64encode(b"file").decode())
    metadata = db.one("SELECT metadata FROM commit_records WHERE collection_id=%s", (cid,))[
        "metadata"
    ]
    assert metadata["author"]["name"] == "Synthetic Developer"
    assert metadata["author"]["timezone"]
    assert metadata["message"] == "first\n"
    assert "intent" not in json.dumps(metadata)


def test_linked_worktree_duplicate_and_detached_head(db, git, tmp_path):
    root = tmp_path / "repo"
    oid = commit(git, root, "a", "a\n", "first")
    linked = tmp_path / "linked"
    git("worktree", "add", "--detach", str(linked), oid)
    with pytest.raises(CollectionError) as detached:
        register(db, linked)
    assert detached.value.code == "checkout_mismatch"
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "completed" and row["snapshot"]["branch"] == "main"
    assert row["summary"]["commits"] == 1
    with pytest.raises(CollectionError, match="이미 등록"):
        register(db, linked)


def test_shallow_history_requires_cleanup_without_fetch(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "a", "a\n", "first")
    commit(git, root, "a", "b\n", "second")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", root.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )
    shallow_before = (shallow / ".git/shallow").read_bytes()
    _, cid = register(db, shallow)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "cleanup_pending" and row["summary"]["commits"] == 1
    assert any(i["code"] == "shallow_history" for i in row["issues"])
    assert (shallow / ".git/shallow").read_bytes() == shallow_before


def test_conflicted_index_preserves_each_stage(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "a", "base\n", "first")
    git("checkout", "-b", "side")
    commit(git, root, "a", "side\n", "side")
    git("checkout", "main")
    commit(git, root, "a", "main\n", "main")
    with pytest.raises(subprocess.CalledProcessError):
        git("merge", "side")
    _, cid = register(db, root)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s AND layer='index'", (cid,))
    assert {r["metadata"]["stage"] for r in rows} == {1, 2, 3}
    assert {bytes(r["body"]) for r in rows} == {b"base\n", b"main\n", b"side\n"}
    collect(db, cid, lambda: False)
    assert db.detail(cid)["state"] == "completed"


def test_changed_snapshot_and_git_output_limit_are_visible(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "a", "a\n", "first")
    original = Git.status
    calls = 0

    def moving_status(self):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "a").write_text("changed during observation\n")
        return original(self)

    monkeypatch.setattr(Git, "status", moving_status)
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "cleanup_pending" and row["snapshot"]["complete"] is False
    assert any(i["code"] == "repository_changed" for i in row["issues"])
    with pytest.raises(CollectionError) as exc:
        Git(root).run("show", "HEAD:a", limit=1)
    assert exc.value.code == "git_output_limit"


def test_status_does_not_execute_clean_or_process_filters(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "a.txt", "hello\n", "first")
    marker = tmp_path / "filter-executed"
    helper = tmp_path / "filter"
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
    helper.chmod(0o755)
    (root / ".gitattributes").write_text("*.txt filter=danger\n")
    git("add", ".gitattributes")
    git("config", "filter.danger.clean", str(helper))
    (root / "a.txt").write_text("HELLO\n")  # Same size forces Git's content comparison.
    before_index = (root / ".git/index").read_bytes()
    assert b"a.txt" in Git(root).status()
    assert not marker.exists()
    # A process filter would require a protocol handshake; it must not start at all.
    git("config", "filter.danger.process", str(helper))
    git("config", "filter.danger.required", "true")
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    assert db.detail(cid)["state"] == "completed"
    assert not marker.exists()
    assert (root / ".git/index").read_bytes() == before_index


def test_diff_path_does_not_include_new_directory_descendants(db, git, tmp_path):
    root = tmp_path / "repo"
    first = commit(git, root, "odd[dir]", "safe text\n", "base")
    git("mv", "odd[dir]", "renamed.txt")
    (root / "odd[dir]").mkdir()
    (root / "odd[dir]/.env").write_text("PRIVATE_FIXTURE_CONTENT\n")
    git("add", ".")
    git("commit", "-m", "rename and directory")
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    rows = db.all(
        "SELECT * FROM change_records WHERE collection_id=%s AND parent_oid=%s", (cid, first)
    )
    rename = next(r for r in rows if r["metadata"]["status"].startswith("R"))
    assert b"safe text" in bytes(rename["diff"])
    assert b"PRIVATE_FIXTURE_CONTENT" not in b"".join(bytes(r["diff"] or b"") for r in rows)
    assert (
        next(r for r in rows if r["metadata"]["new"]["path"].endswith(".env"))["body_reason"]
        == "sensitive_path"
    )


def test_interrupted_snapshot_is_removed_and_new_manual_observes_new_bytes(
    db, git, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    commit(git, root, "a", "head\n", "first")
    (root / "a").write_text("registration bytes\n")
    projects, cid = register(db, root)
    pid = str(db.detail(cid)["project_id"])
    db.execute("DELETE FROM working_entries WHERE collection_id=%s", (cid,))
    db.execute(
        "UPDATE collections SET state='capturing',"
        "snapshot=jsonb_set(snapshot,'{complete}','false') WHERE id=%s",
        (cid,),
    )
    worker = Worker(db)
    monkeypatch.setattr(worker.thread, "start", lambda: None)
    try:
        worker.start()
        assert db.detail(cid)["state"] == "cleanup_pending"
        worker.once()
    finally:
        worker.close()
    assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))
    (root / "a").write_text("later bytes\n")
    new = projects.collect_now(pid)
    collect(db, str(new["id"]), lambda: False)
    assert db.detail(str(new["id"]))["state"] == "completed"
    assert (
        bytes(
            db.one(
                "SELECT body FROM working_records WHERE collection_id=%s AND layer='working'",
                (new["id"],),
            )["body"]
        )
        == b"later bytes\n"
    )


def test_failed_index_blob_keeps_locator_and_other_working_bodies(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "a", "head\n", "base")
    (root / "a").write_text("staged\n")
    git("add", "a")
    staged = git("rev-parse", ":a").decode()
    (root / "a").write_text("working\n")
    original = Git.blob

    def unavailable(self, oid, *args, **kwargs):
        if oid == staged:
            raise CollectionError("git_object_unavailable", "Synthetic unavailable index object")
        return original(self, oid, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Git, "blob", unavailable)
        projects, cid = register(db, root)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    index = next(r for r in rows if r["layer"] == "index")
    assert index["metadata"]["oid"] == staged and index["body"] is None
    assert index["body_reason"] == "git_object_unavailable"
    assert bytes(next(r["body"] for r in rows if r["layer"] == "working")) == b"working\n"
    collect(db, cid, lambda: False)
    assert db.detail(cid)["state"] == "cleanup_pending"
    assert db.detail(cid)["issues"]
    from project_log.collections import Collections

    Collections(db).remove(cid, cleanup=True)
    assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))


def test_snapshot_rejects_branch_change_after_preflight(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "a", "a\n", "first")
    git("branch", "other")
    original = Git.refs
    switched = False

    def moving_refs(self):
        nonlocal switched
        if not switched:
            switched = True
            git("checkout", "other")
        return original(self)

    monkeypatch.setattr(Git, "refs", moving_refs)
    _, cid = register(db, root)
    row = db.detail(cid)
    assert row["snapshot"]["branch"] == "main"
    assert base64.b64decode(row["snapshot"]["branch_ref_b64"]) == b"refs/heads/main"
    assert not row["snapshot"]["complete"]
    assert row["summary"]["working_entries"] == 0
    assert any(i["code"] == "checkout_changed_before_capture" for i in row["issues"])
    worker = Worker(db)
    try:
        assert worker.once()
    finally:
        worker.close()
    assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))


def test_missing_git_blob_is_explicit_and_keeps_change_locator(db, git, tmp_path):
    root = tmp_path / "repo"
    oid = commit(git, root, "a", "body\n", "first")
    blob = git("rev-parse", "HEAD:a").decode()
    projects, cid = register(db, root)
    (root / ".git/objects" / blob[:2] / blob[2:]).unlink()  # Disposable test fixture only.
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "cleanup_pending"
    assert any(i["code"] == "git_object_unavailable" for i in row["issues"])
    assert (
        db.one("SELECT metadata FROM change_records WHERE collection_id=%s", (cid,))["metadata"][
            "new_oid"
        ]
        == blob
    )
    head_file = db.one("SELECT metadata FROM head_records WHERE collection_id=%s", (cid,))[
        "metadata"
    ]
    assert head_file["oid"] == blob and head_file["size_reason"] == "git_object_unavailable"
    assert head_file["size"] is None
    with pytest.raises(CollectionError) as exc:
        projects.source(cid, oid, base64.b64encode(b"a").decode())
    assert exc.value.code == "git_object_unavailable"

    from project_log.collections import Collections

    Collections(db).remove(cid, cleanup=True)
    assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))


def test_history_parent_gap_is_not_completed(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    first = commit(git, root, "a", "first\n", "first")
    last = commit(git, root, "a", "last\n", "last")
    _, cid = register(db, root)
    original = Git.run

    def truncated(self, *args, **kwargs):
        if args[0] == "rev-list":
            return (last + "\n").encode()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Git, "run", truncated)
    collect(db, cid, lambda: False)
    row = db.detail(cid)
    assert row["state"] == "cleanup_pending"
    assert any(
        i["code"] == "history_gap" and first in i["context"]["parents"] for i in row["issues"]
    )


def test_staged_add_and_deletions_have_layer_provenance(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "staged-delete", "original\n", "first")
    commit(git, root, "working-delete", "original\n", "second")
    git("rm", "staged-delete")
    (root / "working-delete").unlink()
    (root / "new.py").write_text("print('new')\n")
    git("add", "new.py")
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert {r["metadata"]["status"] for r in rows} == {"D ", " D", "A "}
    assert all(r["observed_at"] is not None for r in rows)
    assert all(r["sha256"] for r in rows if r["body"] is not None)
    assert (
        next(r for r in rows if r["layer"] == "index" and r["metadata"]["path"] == "new.py")[
            "metadata"
        ]["stage"]
        == 0
    )
    assert db.detail(cid)["state"] == "completed"


def test_registration_does_not_wait_for_history(db, git, tmp_path, monkeypatch):
    import threading

    import project_log.worker as worker_module

    root = tmp_path / "repo"
    commit(git, root, "a", "a\n", "first")
    entered, release = threading.Event(), threading.Event()
    original = worker_module.collect

    def paused_history(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Registration waited for background history"
        original(*args, **kwargs)

    monkeypatch.setattr(worker_module, "collect", paused_history)
    with TestClient(create_app(db)) as client:
        try:
            response = client.post(
                "/api/projects",
                json={"path": str(root), "name": "Async", "status": "new", "base_branch": "main"},
            )
            assert response.status_code == 201
            assert entered.wait(3)
            cid = response.json()["collections"][0]["id"]
            assert client.get(f"/api/collections/{cid}").json()["state"] == "running"
        finally:
            release.set()


def test_inaccessible_path_is_rejected(db, git, tmp_path):
    root = tmp_path / "repo"
    root.chmod(0)
    try:
        with pytest.raises(CollectionError) as exc:
            register(db, root)
        assert exc.value.code == "invalid_path"
    finally:
        root.chmod(0o700)


def test_git_timeout_preserves_failure_code(git, tmp_path):
    with pytest.raises(CollectionError) as exc:
        Git(tmp_path / "repo").run("status", timeout=0)
    assert exc.value.code == "git_timeout"


def test_verification_fingerprint_rejects_symlink_parent(git, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from verify_repository import file_fingerprint

    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("outside bytes must not be read\n")
    (root / "parent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        file_fingerprint(root, b"parent/private")


def test_large_diff_is_documented_policy_exclusion(db, git, tmp_path):
    root = tmp_path / "repo"
    first = commit(git, root, "a", "a\n" * 300000, "first")
    commit(git, root, "a", "b\n" * 300000, "second")
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    row = db.one(
        "SELECT * FROM change_records WHERE collection_id=%s AND parent_oid=%s", (cid, first)
    )
    assert row["diff"] is None and row["body_reason"] == "large_diff"
    assert row["metadata"]["old_oid"] and row["metadata"]["new_oid"]
    assert db.detail(cid)["state"] == "completed"


def test_queued_collection_is_cleaned_on_start_without_resume(db, git, tmp_path):
    _, cid = register(db, tmp_path / "repo")
    assert db.detail(cid)["state"] == "queued"
    with TestClient(create_app(db)) as client:
        for _ in range(100):
            response = client.get(f"/api/collections/{cid}")
            if response.status_code == 404 and not db.all(
                "SELECT id FROM collections WHERE id=%s", (cid,)
            ):
                break
            time.sleep(0.03)
        assert response.status_code == 404
        assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))
