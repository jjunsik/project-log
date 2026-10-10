import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.collector import collect
from project_log.db import put_content
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.project_deletion import ProjectDeletion
from project_log.service import Projects, Registration


def fixture(db, git, tmp_path):
    (tmp_path / "repo/file.txt").write_text("preserved original\n")
    git("add", ".")
    git("commit", "-m", "synthetic")
    p = Projects(db).register(
        Registration(
            path=str(tmp_path / "repo"), name="delete me", status="ongoing", base_branch="main"
        )
    )
    collect(db, str(p["collections"][0]["id"]), lambda: False)
    pid = str(p["id"])
    store = Materials(db)
    rows = [store.add(pid, f"{i}.txt", f"material {i}".encode()) for i in range(2)]
    other = str(uuid4())
    db.execute(
        "INSERT INTO projects(id,name,path,repository_key,status,coding_agent,repository_info) "
        "VALUES (%s,'keep',%s,%s,'new','none','{}')",
        (other, str(tmp_path / "other"), str(tmp_path / "other/.git")),
    )
    same = store.add(other, "same.txt", b"material 0")
    with db.connect() as conn:
        shared = put_content(conn, other, b"preserved original\n")
    return pid, other, store, rows, same, shared


def fingerprints(db):
    return {
        table: db.all(f"SELECT * FROM {table} ORDER BY 1")
        for table in (
            "projects",
            "collections",
            "contents",
            "user_materials",
            "working_entries",
            "commits",
            "changes",
            "head_files",
            "git_commits",
            "git_changes",
            "git_files",
            "schema_migrations",
        )
    }


def test_project_delete_confirmation_ownership_repository_and_repeated_request(db, git, tmp_path):
    pid, other, store, rows, same, shared = fixture(db, git, tmp_path)
    before = {
        p.relative_to(tmp_path / "repo"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "repo").rglob("*")
        if p.is_file()
    }
    ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
    with TestClient(create_app(db, start_worker=False)) as client:
        assert (
            client.request("DELETE", f"/api/projects/{pid}", json={"name": "wrong"}).status_code
            == 409
        )
        assert (
            client.request(
                "DELETE",
                f"/api/projects/{pid}",
                json={"name": "delete me"},
                headers={"Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert client.get(f"/api/projects/{pid}").status_code == 200
        response = client.request("DELETE", f"/api/projects/{pid}", json={"name": "delete me"})
        assert response.status_code == 200, response.text
        assert client.get(f"/api/projects/{pid}").status_code == 404
        assert client.get(f"/api/projects/{other}").status_code == 200
        assert (
            client.request("DELETE", f"/api/projects/{pid}", json={"name": "delete me"}).status_code
            == 404
        )
    assert store.read(other, str(same["id"]))[1] == b"material 0"
    assert (
        db.one("SELECT body FROM contents WHERE id=%s", (shared,))["body"]
        == b"preserved original\n"
    )
    for table in (
        "collections",
        "contents",
        "user_materials",
        "git_commits",
        "git_changes",
        "git_files",
        "working_repairs",
        "commits",
        "changes",
        "head_files",
        "working_entries",
    ):
        assert not db.all(f"SELECT * FROM {table} WHERE project_id=%s", (pid,))
    assert all(not store.path(pid, str(r["id"])).exists() for r in rows)
    assert db.all("SELECT * FROM schema_migrations ORDER BY version") == ledger
    assert before == {
        p.relative_to(tmp_path / "repo"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "repo").rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "state",
    ["capturing", "queued", "running", "cancel_pending", "cleanup_pending", "failed", "partial"],
)
def test_busy_project_rejected_without_changes(db, git, tmp_path, state):
    pid, _, store, rows, _, _ = fixture(db, git, tmp_path)
    db.execute("UPDATE collections SET state=%s WHERE project_id=%s", (state, pid))
    before = fingerprints(db)
    with pytest.raises(CollectionError) as error:
        ProjectDeletion(db, store).delete(pid, "delete me")
    assert error.value.code == "conflict"
    assert fingerprints(db) == before
    assert all(
        store.read(pid, str(r["id"]))[1] == f"material {i}".encode() for i, r in enumerate(rows)
    )


def test_second_copy_unlink_failure_rolls_back_all_records_and_copies(
    db, git, tmp_path, monkeypatch
):
    pid, _, store, rows, _, _ = fixture(db, git, tmp_path)
    before = fingerprints(db)
    target = store.path(pid, str(rows[1]["id"]))
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == target:
            raise OSError("synthetic unlink failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError):
        ProjectDeletion(db, store).delete(pid, "delete me")
    assert fingerprints(db) == before
    assert all(
        store.read(pid, str(r["id"]))[1] == f"material {i}".encode() for i, r in enumerate(rows)
    )


def test_db_failure_restores_and_commit_cleanup_failure_is_not_success(
    db, git, tmp_path, monkeypatch
):
    pid, _, store, rows, _, _ = fixture(db, git, tmp_path)
    before = fingerprints(db)
    deletion = ProjectDeletion(db, store)
    real = deletion._records

    def fail(conn, project):
        real(conn, project)
        raise RuntimeError("synthetic DB-stage failure")

    monkeypatch.setattr(deletion, "_records", fail)
    with pytest.raises(RuntimeError):
        deletion.delete(pid, "delete me")
    assert fingerprints(db) == before
    monkeypatch.setattr(deletion, "_records", real)
    unlink = Path.unlink
    backup = deletion.root / pid / str(rows[0]["id"])

    def fail_cleanup(path, *args, **kwargs):
        if path == backup:
            raise OSError("synthetic post-commit cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(CollectionError) as exc:
        deletion.delete(pid, "delete me")
    assert exc.value.code == "storage_failed"
    assert not db.all("SELECT id FROM projects WHERE id=%s", (pid,))
    assert (deletion.root / pid / "manifest.json").exists()
    monkeypatch.setattr(Path, "unlink", unlink)
    deletion.delete(pid, "delete me")
    assert not (deletion.root / pid).exists()


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_deletion_recovers_from_durable_db_outcome(db, git, tmp_path, committed):
    pid, other, store, rows, same, _ = fixture(db, git, tmp_path)
    deletion = ProjectDeletion(db, store)
    stored = [
        {k: str(r[k]) if k == "id" else r[k] for k in ("id", "size_bytes", "sha256")} for r in rows
    ]
    deletion._prepare(pid, "delete me", stored)
    for r in rows:
        store.path(pid, str(r["id"])).unlink()
    if committed:
        with db.connect() as conn:
            deletion._records(conn, pid)
    ProjectDeletion(db, Materials(db)).recover()
    assert not (deletion.root / pid).exists()
    assert store.read(other, str(same["id"]))[1] == b"material 0"
    if not committed:
        assert all(
            store.read(pid, str(r["id"]))[1] == f"material {i}".encode() for i, r in enumerate(rows)
        )
    else:
        assert not db.all("SELECT id FROM projects WHERE id=%s", (pid,))


def test_concurrent_delete_has_one_success_and_no_unrelated_loss(db, git, tmp_path):
    pid, other, store, _, same, _ = fixture(db, git, tmp_path)

    def remove():
        try:
            ProjectDeletion(db, store).delete(pid, "delete me")
            return "deleted"
        except CollectionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: remove(), range(2))) == ["deleted", "not_found"]
    assert store.read(other, str(same["id"]))[1] == b"material 0"


def test_symlink_storage_and_foreign_baseline_are_rejected(db, git, tmp_path):
    pid, other, store, rows, _, _ = fixture(db, git, tmp_path)
    cid = db.one("SELECT id FROM collections WHERE project_id=%s", (pid,))["id"]
    db.execute(
        "INSERT INTO collections(id,project_id,state,baseline_id,policy) "
        "VALUES (%s,%s,'completed',%s,'{}')",
        (str(uuid4()), other, cid),
    )
    with pytest.raises(CollectionError) as exc:
        ProjectDeletion(db, store).delete(pid, "delete me")
    assert exc.value.code == "conflict"
    db.execute("UPDATE collections SET baseline_id=NULL WHERE project_id=%s", (other,))
    target = store.path(pid, str(rows[0]["id"]))
    target.unlink()
    original = tmp_path / "repo/file.txt"
    target.symlink_to(original)
    before = fingerprints(db)
    with pytest.raises(OSError):
        ProjectDeletion(db, store).delete(pid, "delete me")
    assert original.read_text() == "preserved original\n"
    assert fingerprints(db) == before


def test_incomplete_staged_copy_recovers_without_replacing_originals(db, git, tmp_path):
    pid, _, store, rows, _, _ = fixture(db, git, tmp_path)
    deletion = ProjectDeletion(db, store)
    stored = [
        {k: str(r[k]) if k == "id" else r[k] for k in ("id", "size_bytes", "sha256")} for r in rows
    ]
    deletion._prepare(pid, "delete me", stored)
    (deletion.root / pid / str(rows[0]["id"])).write_bytes(b"partial staged write")
    (deletion.root / pid / str(rows[1]["id"])).unlink()
    deletion.recover()
    assert all(
        store.read(pid, str(r["id"]))[1] == f"material {i}".encode() for i, r in enumerate(rows)
    )


def test_deletion_serializes_pending_memo_and_upload(db, git, tmp_path, monkeypatch):
    from threading import Event

    from project_log.collections import Collections, Memo

    pid, other, store, _, same, _ = fixture(db, git, tmp_path)
    cid = str(db.one("SELECT id FROM collections WHERE project_id=%s", (pid,))["id"])
    deletion = ProjectDeletion(db, store)
    entered, release = Event(), Event()
    original = deletion._records

    def held(conn, project):
        entered.set()
        assert release.wait(5)
        original(conn, project)

    monkeypatch.setattr(deletion, "_records", held)

    def attempt(action):
        try:
            action()
            return "unexpected success"
        except CollectionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=3) as pool:
        deleting = pool.submit(deletion.delete, pid, "delete me")
        assert entered.wait(5)
        memo = pool.submit(attempt, lambda: Collections(db).memo(cid, Memo(title="late")))
        upload = pool.submit(attempt, lambda: store.add(pid, "late.txt", b"late upload"))
        release.set()
        deleting.result(timeout=10)
        assert memo.result(timeout=10) == upload.result(timeout=10) == "not_found"
    assert store.read(other, str(same["id"]))[1] == b"material 0"


def test_cleanup_failure_api_reports_incomplete_and_retry_finishes(db, git, tmp_path, monkeypatch):
    pid, other, store, rows, same, _ = fixture(db, git, tmp_path)
    backup = store.root / ".project-deletions" / pid / str(rows[0]["id"])
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == backup:
            raise OSError("synthetic cleanup denial")
        return unlink(path, *args, **kwargs)

    with TestClient(create_app(db, start_worker=False)) as client:
        monkeypatch.setattr(Path, "unlink", fail)
        response = client.request("DELETE", f"/api/projects/{pid}", json={"name": "delete me"})
        assert response.status_code == 503 and "정리" in response.json()["detail"]
        assert client.get(f"/api/projects/{pid}").status_code == 404
        assert backup.exists()
        monkeypatch.setattr(Path, "unlink", unlink)
        assert (
            client.request("DELETE", f"/api/projects/{pid}", json={"name": "delete me"}).status_code
            == 200
        )
    assert not backup.parent.exists()
    assert store.read(other, str(same["id"]))[1] == b"material 0"
