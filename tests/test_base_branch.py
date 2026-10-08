import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from project_log.api import create_app
from project_log.collector import collect
from project_log.db import Database
from project_log.git import CollectionError, Git
from project_log.service import Projects, Registration, Settings
from project_log.worker import Worker
from scripts.verification_db import temporary_database
from tests.test_collection import commit


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("main", "main"),
        ("both", "main"),
        ("master", "master"),
        ("remote-main", None),
        ("remote-master", None),
        ("custom", None),
        ("unborn", "start/here"),
    ],
)
def test_initial_suggestion_uses_only_local_branches(db, git, tmp_path, case, expected):
    root = tmp_path / "repo"
    if case == "unborn":
        git("symbolic-ref", "HEAD", "refs/heads/start/here")
    else:
        tip = commit(git, root, "README", "readme\n", "first")
        if case == "both":
            git("branch", "master")
        if case == "master":
            git("branch", "-m", "master")
        if case in {"custom", "remote-main", "remote-master"}:
            git("branch", "-m", "trunk")
        if case.startswith("remote-"):
            git("update-ref", f"refs/remotes/origin/{case.removeprefix('remote-')}", tip)
        git("checkout", "-b", "feature")
    before = (Git(root).refs(), Git(root).symbolic_head(), Git(root).index(), Git(root).status())
    with TestClient(create_app(db, start_worker=False)) as client:
        response = client.post("/api/repositories/diagnose", json={"path": str(root)})
        assert response.status_code == 200
        assert response.json()["suggested_base_branch"] == expected
    assert before == (
        Git(root).refs(),
        Git(root).symbolic_head(),
        Git(root).index(),
        Git(root).status(),
    )


@pytest.mark.parametrize(
    "name", ["", "  ", "bad name", "a..b", "bad.lock", "-bad", "@{-1}", "missing", "origin/main"]
)
def test_invalid_or_absent_branch_does_not_register_or_fallback(db, git, tmp_path, name):
    root = tmp_path / "repo"
    tip = commit(git, root, "README", "no source\n", "first")
    git("update-ref", "refs/remotes/origin/main", tip)
    with TestClient(create_app(db, start_worker=False)) as client:
        response = client.post(
            "/api/projects",
            json={
                "path": str(root),
                "name": "Invalid",
                "status": "completed",
                "base_branch": name,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] in {
            "base_branch_required",
            "invalid_base_branch",
            "base_branch_not_found",
        }
        assert "브랜치" in response.json()["detail"]
    assert db.one("SELECT count(*) AS n FROM projects")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM collections")["n"] == 0


def test_unborn_branch_with_other_reachable_commits_is_not_a_zero_commit_repo(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "README", "readme\n", "first")
    git("symbolic-ref", "HEAD", "refs/heads/not-yet-created")
    with pytest.raises(CollectionError) as error:
        Projects(db).register(
            Registration(path=str(root), name="Unborn", status="new", base_branch="not-yet-created")
        )
    assert error.value.code == "base_branch_not_found"
    assert Git(root).suggested_base_branch() == "main"


def test_checkout_change_during_preflight_is_not_a_stable_mismatch(git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "README", "readme\n", "first")
    observations = iter([b"refs/heads/main", b"refs/heads/feature"])
    monkeypatch.setattr(Git, "symbolic_head", lambda self: next(observations))
    with pytest.raises(CollectionError) as error:
        Git(root).collection_branch("main")
    assert error.value.code == "repository_changed"
    assert "검사 중 checkout이 바뀌었습니다" in error.value.message


@pytest.mark.parametrize("status", ["new", "ongoing", "completed"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_all_statuses_register_change_and_collect_without_evidence_minimum(
    db, git, tmp_path, status, count
):
    root = tmp_path / "repo"
    git("symbolic-ref", "HEAD", "refs/heads/delivery")
    for index in range(count):
        commit(git, root, "README", f"text {index}\n", f"commit {index}")
    projects = Projects(db)
    project = projects.register(
        Registration(path=str(root), name="Declared", status=status, base_branch="delivery")
    )
    pid, cid = str(project["id"]), str(project["collections"][0]["id"])
    collect(db, cid, lambda: False)
    initial = db.detail(cid)
    assert initial["state"] == "completed", initial["issues"]
    assert initial["summary"]["commits"] == count
    assert initial["snapshot"]["branch"] == "delivery"
    assert base64.b64decode(initial["snapshot"]["branch_ref_b64"]) == b"refs/heads/delivery"
    assert (initial["snapshot"]["head"] is None) == (count == 0)
    for next_status in ("new", "ongoing", "completed"):
        result = projects.update(
            pid, Settings(name="Declared", status=next_status, base_branch="delivery")
        )
        assert result["status"] == next_status
        assert (
            len(result["collections"]) == 1
        )  # Status changes do not create validation Collections.
    manual = projects.collect_now(pid)
    collect(db, str(manual["id"]), lambda: False)
    assert db.detail(str(manual["id"]))["state"] == "completed"
    assert projects.project(pid)["status"] == "completed"


def test_source_presence_is_not_a_gate_and_branch_configuration_is_atomic(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "app.py", "print('hello')\n", "source")
    git("branch", "release/검토")
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="Original", status="completed", base_branch="main")
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    with pytest.raises(CollectionError) as active:
        projects.update(pid, Settings(name="Changed", status="new", base_branch="release/검토"))
    assert active.value.code == "conflict"
    assert projects.project(pid)["base_branch"] == "main"
    collect(db, cid, lambda: False)
    for name in ("bad..name", "missing", " "):
        with pytest.raises(CollectionError):
            projects.update(pid, Settings(name="Must roll back", status="new", base_branch=name))
        assert projects.project(pid)["name"] == "Original"
        assert projects.project(pid)["status"] == "completed"
        assert projects.project(pid)["base_branch"] == "main"
    projects.update(pid, Settings(name="Original", status="completed", base_branch="release/검토"))
    before = (
        Git(root).symbolic_head(),
        Git(root).head(),
        Git(root).refs(),
        Git(root).status(),
        Git(root).index(),
        (root / "app.py").read_bytes(),
    )
    with pytest.raises(CollectionError) as mismatch:
        projects.collect_now(pid)
    assert mismatch.value.code == "checkout_mismatch"
    assert '기준 브랜치: "release/검토"' in mismatch.value.message
    assert '현재 checkout: "main"' in mismatch.value.message
    assert "checkout한 뒤 다시 수집" in mismatch.value.message
    assert len(projects.project(pid)["collections"]) == 1
    assert before == (
        Git(root).symbolic_head(),
        Git(root).head(),
        Git(root).refs(),
        Git(root).status(),
        Git(root).index(),
        (root / "app.py").read_bytes(),
    )
    git("checkout", "release/검토")  # Synthetic fixture only, never a Product operation.
    new = projects.collect_now(pid)
    collect(db, str(new["id"]), lambda: False)
    assert db.detail(str(new["id"]))["state"] == "completed"
    assert db.detail(str(new["id"]))["snapshot"]["branch"] == "release/검토"
    assert db.detail(cid)["snapshot"]["branch"] == "main"
    assert db.detail(cid)["snapshot"]["head"] == db.detail(str(new["id"]))["snapshot"]["head"]
    assert str(new["baseline_id"]) == cid


def test_mismatch_and_detached_head_do_not_create_initial_collections(db, git, tmp_path):
    root = tmp_path / "repo"
    tip = commit(git, root, "README", "readme\n", "first")
    git("checkout", "-b", "feature")
    projects = Projects(db)
    for detached in (False, True):
        if detached:
            git("checkout", "--detach", tip)
        with pytest.raises(CollectionError) as error:
            projects.register(
                Registration(path=str(root), name="Mismatch", status="new", base_branch="main")
            )
        assert error.value.code == "checkout_mismatch"
        assert ("분리된 HEAD" if detached else "feature") in error.value.message
    assert db.one("SELECT count(*) AS n FROM collections")["n"] == 0


def test_invalid_existing_branch_blocks_only_its_project(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "README", "readme\n", "first")
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="Original", status="new", base_branch="main")
    )
    cid, pid = str(p["collections"][0]["id"]), str(p["id"])
    collect(db, cid, lambda: False)
    git("branch", "-m", "replacement")
    with pytest.raises(CollectionError) as error:
        projects.collect_now(pid)
    assert error.value.code == "base_branch_not_found"
    other = tmp_path / "another"
    other.mkdir()
    Git(other).run("init", "-b", "trunk")
    second = projects.register(
        Registration(path=str(other), name="Another", status="completed", base_branch="trunk")
    )
    collect(db, str(second["collections"][0]["id"]), lambda: False)
    assert projects.project(str(second["id"]))["collections"][0]["state"] == "completed"
    assert len(projects.project(pid)["collections"]) == 1


def test_003_upgrade_preserves_legacy_unknown_then_cleans_failure_and_collects(git, tmp_path):
    root = tmp_path / "repo"
    pid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for file in sorted(Path("src/project_log/migrations").glob("*.sql"))[:3]:
                raw = file.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (file.name, hashlib.sha256(raw).hexdigest()),
                )
            git_info = {
                "path": str(root),
                "repository_key": str(root / ".git"),
                "branch": "old-registration-metadata",
            }
            conn.execute(
                "INSERT INTO projects(id,name,path,repository_key,status,"
                "coding_agent,repository_info) "
                "VALUES (%s,'Legacy',%s,%s,'ongoing','none',%s)",
                (pid, str(root), str(root / ".git"), Jsonb(git_info)),
            )
            conn.execute(
                "INSERT INTO collections(id,project_id,state,snapshot,policy) "
                "VALUES (%s,%s,'failed','{}','{}')",
                (cid, pid),
            )
        before_project = db.one("SELECT * FROM projects WHERE id=%s", (pid,))
        before_collection = db.detail(cid)
        db.migrate()
        db.migrate()
        migrated = db.one("SELECT * FROM projects WHERE id=%s", (pid,))
        assert {k: migrated[k] for k in before_project} == before_project
        assert migrated["base_branch"] is None
        assert {k: db.detail(cid)[k] for k in before_collection} == before_collection
        assert db.one("SELECT count(*) AS n FROM schema_migrations")["n"] == 6
        projects = Projects(db)
        worker = Worker(db)
        try:
            assert worker.once()
        finally:
            worker.close()
        with pytest.raises(CollectionError) as error:
            projects.collect_now(pid)
        assert error.value.code == "base_branch_required"
        assert projects.project(pid)["collections"] == []
        projects.update(pid, Settings(name="Legacy", status="completed", base_branch="main"))
        manual = projects.collect_now(pid)
        collect(db, str(manual["id"]), lambda: False)
        snapshot = db.detail(str(manual["id"]))["snapshot"]
        assert snapshot["branch"] == "main" and snapshot["head"] is None
        assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))


def test_manual_uses_new_base_branch_and_preserves_completed_original(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "README", "original\n", "first")
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="Retry", status="new", base_branch="main")
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    original = db.detail(cid)["snapshot"]
    git("branch", "delivery")
    projects.update(pid, Settings(name="Retry", status="completed", base_branch="delivery"))
    with pytest.raises(CollectionError) as error:
        projects.collect_now(pid)
    assert error.value.code == "checkout_mismatch"
    assert len(projects.project(pid)["collections"]) == 1
    git("checkout", "delivery")
    (root / "README").write_text("NOW\n")
    retry = projects.collect_now(pid)
    collect(db, str(retry["id"]), lambda: False)
    assert db.detail(str(retry["id"]))["snapshot"]["branch"] == "delivery"
    bodies = db.all("SELECT body FROM working_records WHERE collection_id=%s", (retry["id"],))
    assert any(bytes(row["body"]) == b"NOW\n" for row in bodies)
    assert db.detail(cid)["snapshot"] == original
