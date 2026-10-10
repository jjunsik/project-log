import base64
import hashlib
import shutil
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from project_log.api import create_app
from project_log.browser import Browser
from project_log.collections import Memo
from project_log.collector import collect
from project_log.db import Database
from project_log.folders import Folders
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.service import Projects, Registration, Settings
from scripts.verification_db import temporary_database


def completed(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "a.txt").write_text("baseline\n")
    (root / "docs/nested").mkdir(parents=True)
    (root / "docs/nested/a.md").write_text("historical document\n")
    (root / ".git/info/exclude").write_text("/docs/\n")
    git("add", ".")
    git("commit", "-m", "subject\n\nbody")
    projects = Projects(db)
    p = projects.register(
        Registration(name="test", path=str(root), status="ongoing", base_branch="main")
    )
    cid = str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    return projects, str(p["id"]), cid


def settings(path, **values):
    return Settings(
        name="renamed", status="completed", base_branch="main", path=str(path), **values
    )


@pytest.mark.parametrize("kind", ["move", "copy", "restart"])
def test_reconnect_preserves_project_history_and_materials(db, git, tmp_path, kind):
    projects, pid, cid = completed(db, git, tmp_path)
    material = Materials(db).add(pid, "extra.txt", b"Project owned copy")
    before = db.detail(cid)
    contents = db.all("SELECT * FROM contents ORDER BY id")
    destination = tmp_path / "destination"
    if kind == "move":
        (tmp_path / "repo").rename(destination)
    else:
        shutil.copytree(tmp_path / "repo", destination)
    if kind == "restart":
        (tmp_path / "repo").rename(tmp_path / "unavailable-old-path")
        projects = Projects(Database(db.dsn))
    review = projects.review_path(pid, settings(destination))
    assert review["missing_count"] == 0
    updated = projects.update(pid, settings(destination))
    assert str(updated["id"]) == pid and updated["path"] == str(destination)
    assert updated["name"] == "renamed" and updated["status"] == "completed"
    assert db.detail(cid) == before
    assert db.all("SELECT * FROM contents ORDER BY id") == contents
    assert Materials(db).read(pid, str(material["id"]))[1] == b"Project owned copy"
    oid = before["snapshot"]["head"]
    assert projects.source(cid, oid, base64.b64encode(b"a.txt").decode())["body"] == "baseline\n"
    assert (
        updated["repository_info"]
        == db.one("SELECT repository_info FROM projects WHERE id=%s", (pid,))["repository_info"]
    )
    new = projects.collect_now(pid)
    collect(db, str(new["id"]), lambda: False)
    assert db.detail(str(new["id"]))["snapshot"]["path"] == str(destination)
    assert db.detail(cid)["snapshot"]["path"] == str(tmp_path / "repo")


def test_copy_then_new_commits_is_a_continuation(db, git, tmp_path):
    projects, pid, _ = completed(db, git, tmp_path)
    copy = tmp_path / "copy"
    shutil.copytree(tmp_path / "repo", copy)
    from project_log.git import Git

    g = Git(copy)
    # Only a synthetic test repository is modified.
    (copy / "new.txt").write_text("new")
    g.run("add", ".")
    g.run("commit", "-m", "continued development")
    assert projects.review_path(pid, settings(copy))["missing_count"] == 0
    projects.update(pid, settings(copy))


def test_copied_linked_worktree_with_broken_admin_link_is_rejected(db, git, tmp_path):
    projects, pid, _ = completed(db, git, tmp_path)
    linked = tmp_path / "linked"
    git("worktree", "add", "-b", "linked", str(linked))
    copy = tmp_path / "linked-copy"
    shutil.copytree(linked, copy)
    with pytest.raises(CollectionError) as caught:
        projects.update(
            pid, Settings(name="test", status="ongoing", base_branch="linked", path=str(copy))
        )
    assert caught.value.code == "worktree_invalid"
    assert projects.project(pid)["path"] == str(tmp_path / "repo")


def test_unrelated_damage_duplicate_busy_and_storage_are_atomic(db, git, tmp_path):
    projects, pid, _ = completed(db, git, tmp_path)
    before = projects.project(pid)
    from project_log.git import Git

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    u = Git(unrelated)
    u.run("init", "-b", "main")
    u.run("config", "user.name", "other")
    u.run("config", "user.email", "other@example.invalid")
    (unrelated / "a.txt").write_text("unrelated")
    u.run("add", ".")
    u.run("commit", "-m", "other")
    with pytest.raises(CollectionError):
        projects.update(pid, settings(unrelated))
    duplicate = projects.register(
        Registration(
            name="other", status="new", base_branch="main", path=str(unrelated), collect=False
        )
    )
    with pytest.raises(CollectionError) as caught:
        projects.update(pid, settings(unrelated))
    assert caught.value.code == "duplicate"
    target = tmp_path / "copy"
    shutil.copytree(tmp_path / "repo", target)
    (target / ".git/HEAD").unlink()
    with pytest.raises(CollectionError):
        projects.update(pid, settings(target))
    shutil.copyfile(tmp_path / "repo/.git/HEAD", target / ".git/HEAD")
    pending = projects.collect_now(pid)
    with pytest.raises(CollectionError) as busy:
        projects.update(pid, settings(target))
    assert busy.value.code == "conflict"
    assert projects.project(pid)["path"] == before["path"]
    assert projects.project(pid)["name"] == before["name"]
    collect(db, str(pending["id"]), lambda: False)
    Materials(db, target / "storage").check_repository(str(tmp_path / "repo"))
    with pytest.raises(CollectionError):
        Materials(db, target / "storage").check_repository(str(target))
    assert projects.project(str(duplicate["id"]))["path"] == str(unrelated)


def test_missing_only_old_objects_requires_exact_warning_confirmation(db, git, tmp_path):
    projects, pid, initial = completed(db, git, tmp_path)
    git("checkout", "-b", "discarded")
    (tmp_path / "repo/old.txt").write_text("old branch")
    git("add", ".")
    git("commit", "-m", "discarded history")
    projects.update(pid, Settings(name="test", status="ongoing", base_branch="discarded"))
    old = projects.collect_now(pid)
    collect(db, str(old["id"]), lambda: False)
    old_oid = db.detail(str(old["id"]))["snapshot"]["head"]
    git("checkout", "main")
    git("branch", "-D", "discarded")
    projects.update(pid, Settings(name="test", status="ongoing", base_branch="main"))
    latest = projects.collect_now(pid)
    collect(db, str(latest["id"]), lambda: False)
    copy = tmp_path / "copy"
    shutil.copytree(tmp_path / "repo", copy)
    from project_log.git import Git

    g = Git(copy)
    g.run("reflog", "expire", "--expire=now", "--all")
    g.run("gc", "--prune=now")
    before = db.all("SELECT * FROM collections ORDER BY id")
    review = projects.review_path(pid, settings(copy))
    assert review["missing_count"] > 0 and old_oid in review["missing_objects"]
    assert "이미 보존된" in review["message"]
    for token in [None, "wrong"]:
        with pytest.raises(CollectionError) as caught:
            projects.update(pid, settings(copy, acknowledged_warnings=token))
        assert caught.value.code == "confirmation_required"
        assert projects.project(pid)["path"] == str(tmp_path / "repo")
    projects.update(pid, settings(copy, acknowledged_warnings=review["warning_token"]))
    assert db.all("SELECT * FROM collections ORDER BY id") == before
    with pytest.raises(CollectionError) as unavailable:
        projects.source(str(old["id"]), old_oid, base64.b64encode(b"old.txt").decode())
    assert unavailable.value.code == "git_object_unavailable"
    assert (
        projects.source(
            initial, db.detail(initial)["snapshot"]["head"], base64.b64encode(b"a.txt").decode()
        )["body"]
        == "baseline\n"
    )


def test_memo_boundaries_unicode_and_database_constraint(db, git, tmp_path):
    _, _, cid = completed(db, git, tmp_path)
    assert Memo(title="😀" * 50, description="한" * 200).title == "😀" * 50
    for value in [{"title": "a" * 51}, {"description": "a" * 201}]:
        with pytest.raises(ValidationError):
            Memo(**value)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE collections SET title=%s WHERE id=%s", ("a" * 51, cid))
    assert db.detail(cid)["title"] is None


def test_007_migration_does_not_truncate_legacy_memos():
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,"
                "sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for file in sorted(Path("src/project_log/migrations").glob("*.sql"))[:6]:
                raw = file.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (file.name, hashlib.sha256(raw).hexdigest()),
                )
            conn.execute(
                "INSERT INTO projects(id,name,path,repository_key,status,"
                "coding_agent,repository_info) "
                "VALUES ('00000000-0000-0000-0000-000000000001','old','/old','/old/.git',"
                "'new','none','{}')"
            )
            conn.execute(
                "INSERT INTO collections(id,project_id,state,policy,title) "
                "VALUES ('00000000-0000-0000-0000-000000000002',"
                "'00000000-0000-0000-0000-000000000001','completed','{}',%s)",
                ("a" * 51,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            db.migrate()
        assert db.one("SELECT title FROM collections")["title"] == "a" * 51
        assert len(db.all("SELECT * FROM schema_migrations")) == 6
        db.execute("UPDATE collections SET title=%s,description=%s", ("a" * 50, "b" * 200))
        db.migrate()
        db.migrate()
        assert db.one("SELECT title,description FROM collections") == {
            "title": "a" * 50,
            "description": "b" * 200,
        }


def test_home_authorization_symlinks_and_replacement(db, tmp_path):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "visible").mkdir()
    (home / "escape").symlink_to(outside, target_is_directory=True)
    folders = Folders(db, home)
    try:
        folders.authorize(str(home / "visible"))
        for path in [home / "../outside", home / "escape", outside]:
            with pytest.raises(CollectionError):
                folders.authorize(str(path))
        home.rename(tmp_path / "previous-home")
        home.mkdir()
        with pytest.raises(CollectionError):
            folders.authorize(str(home))
    finally:
        folders.close()


def test_retired_folder_endpoints_and_native_separate_registration(db, git, tmp_path, monkeypatch):
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    with TestClient(create_app(db, start_worker=False, folder_home=home)) as client:
        assert client.get("/api/folders?relative=../repo").status_code == 404
        assert client.get("/api/folders/roots").status_code == 404
        assert client.post("/api/folders/roots", json={"path": str(tmp_path)}).status_code == 405
        assert (
            client.post(
                "/api/repositories/diagnose", json={"path": str(tmp_path / "repo")}
            ).status_code
            == 422
        )
        real_run = subprocess.run

        def choose_only(args, **kwargs):
            if args[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(
                    args, 0, (str(tmp_path / "repo") + "/\n").encode(), b""
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr("project_log.native_picker.subprocess.run", choose_only)
        token = client.get("/api/folders/picker").json()["token"]
        response = client.post(
            "/api/folders/picker",
            json={"token": token},
            headers={"Origin": "http://localhost:8000", "X-Project-Log-Intent": "choose-folder"},
        )
        assert response.status_code == 200 and response.json()["path"] == str(tmp_path / "repo")
        response = client.post(
            "/api/projects",
            json={
                "path": str(tmp_path / "repo"),
                "name": "empty",
                "status": "new",
                "base_branch": "main",
                "collect": False,
            },
        )
        assert response.status_code == 201 and response.json()["collections"] == []
        pid = response.json()["id"]
        git("checkout", "-b", "other")
        assert client.post(f"/api/projects/{pid}/collections", json={}).status_code == 422
        assert client.get(f"/api/projects/{pid}").json()["id"] == pid


def test_flat_documents_use_observed_metadata_and_ten_item_pages(db, git, tmp_path):
    _, _, cid = completed(db, git, tmp_path)
    (tmp_path / "repo/docs/nested/a.md").write_text("current replacement")
    page = Browser(db).documents(cid, 10, 0)
    assert page["total"] == 1 and page["items"][0]["name"] == "nested/a.md"
    o = next(v for v in page["items"][0]["observations"] if v["layer"] == "document")
    body = db.one("SELECT body FROM working_records WHERE id=%s", (o["id"],))["body"]
    assert bytes(body) == b"historical document\n"
    assert o["metadata"]["mtime_ns"] > 0 and o["metadata"]["size"] == len(body)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_current_git_object_damage_is_not_downgraded_to_historical_warning(
    db, git, tmp_path, damage
):
    projects, pid, _ = completed(db, git, tmp_path)
    blob = git("rev-parse", "HEAD:a.txt").decode()
    copy = tmp_path / "damaged-copy"
    shutil.copytree(tmp_path / "repo", copy)
    object_file = copy / ".git/objects" / blob[:2] / blob[2:]
    if damage == "missing":
        object_file.unlink()
    else:
        object_file.chmod(0o600)
        object_file.write_bytes(b"corrupt object")
    before = projects.project(pid)
    with pytest.raises(CollectionError) as error:
        projects.update(pid, settings(copy))
    assert error.value.code == "repository_invalid"
    assert projects.project(pid) == before


def test_promised_current_objects_are_not_allowed_as_historical_missing(db, git, tmp_path):
    projects, pid, _ = completed(db, git, tmp_path)
    copy = tmp_path / "partial-copy"
    shutil.copytree(tmp_path / "repo", copy)
    from project_log.git import Git

    g = Git(copy)
    head = g.run("rev-parse", "HEAD").strip()
    tree = g.run("rev-parse", "HEAD^{tree}").strip()
    pack = g.run("pack-objects", "--stdout", input_bytes=head + b"\n" + tree + b"\n")
    g.run("index-pack", "--stdin", "--promisor", input_bytes=pack)
    g.run("config", "remote.origin.promisor", "true")
    g.run("config", "extensions.partialClone", "origin")
    blob = g.run("rev-parse", "HEAD:a.txt").decode().strip()
    (copy / ".git/objects" / blob[:2] / blob[2:]).unlink()
    # A promised object is not an fsck failure, but this app must not fetch it.
    g.run("fsck", "--full", "--no-reflogs", "--no-dangling")
    before = projects.project(pid)
    with pytest.raises(CollectionError) as error:
        projects.update(pid, settings(copy))
    assert error.value.code == "repository_invalid"
    assert projects.project(pid) == before


def test_insufficient_evidence_and_stale_settings_are_rejected(db, git, tmp_path):
    projects, pid, _ = completed(db, git, tmp_path)
    before = projects.project(pid)
    projects.update(pid, Settings(name="newer setting", status="ongoing", base_branch="main"))
    with pytest.raises(CollectionError) as conflict:
        projects.update(
            pid,
            Settings(
                name="stale",
                status="completed",
                base_branch="main",
                expected_updated_at=before["updated_at"],
            ),
        )
    assert conflict.value.code == "conflict"
    assert projects.project(pid)["name"] == "newer setting"
    empty = tmp_path / "empty"
    empty.mkdir()
    from project_log.git import Git

    g = Git(empty)
    g.run("init", "-b", "main")
    p = projects.register(
        Registration(name="empty", status="new", base_branch="main", path=str(empty), collect=False)
    )
    destination = tmp_path / "empty-copy"
    shutil.copytree(empty, destination)
    with pytest.raises(CollectionError) as evidence:
        projects.update(str(p["id"]), settings(destination))
    assert evidence.value.code == "continuity_unavailable"


def test_validation_race_and_database_failure_roll_back_all_settings(
    db, git, tmp_path, monkeypatch
):
    projects, pid, _ = completed(db, git, tmp_path)
    destination = tmp_path / "copy"
    shutil.copytree(tmp_path / "repo", destination)
    before = projects.project(pid)
    import project_log.service as service

    original = service.diagnose
    calls = 0

    def changed(path):
        nonlocal calls
        git_object, info = original(path)
        calls += 1
        if calls == 2:
            info["head"] = "0" * 40
        return git_object, info

    monkeypatch.setattr(service, "diagnose", changed)
    with pytest.raises(CollectionError) as race:
        projects.update(pid, settings(destination))
    assert race.value.code == "repository_changed"
    assert projects.project(pid) == before
    monkeypatch.setattr(service, "diagnose", original)
    db.execute("ALTER TABLE projects ADD CONSTRAINT synthetic_failure CHECK (name<>'renamed')")
    with pytest.raises(psycopg.errors.CheckViolation):
        projects.update(pid, settings(destination))
    assert projects.project(pid) == before
