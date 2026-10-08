import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from project_log.api import create_app
from project_log.collector import collect
from project_log.db import Database
from project_log.git import CollectionError
from project_log.policy import MAX_BODY
from project_log.service import Settings
from scripts.verification_db import temporary_database
from tests.test_collection import commit, register


def finish(db, cid):
    collect(db, cid, lambda: False)
    result = db.detail(cid)
    assert result["state"] == "completed", result["issues"]
    return result


def observe(db, projects, pid):
    row = projects.collect_now(pid)
    return finish(db, str(row["id"]))


def inventory(db, cid):
    return {
        (r["layer"], r["metadata"]["path"], r["metadata"].get("stage", 0)): r
        for r in db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    }


def storage_counts(db):
    return {
        table: db.one(f"SELECT count(*) AS n FROM {table}")["n"]
        for table in ("contents", "git_commits", "git_changes", "git_files")
    }


def test_repeated_observation_dedup_changed_new_deleted_and_commit(db, git, tmp_path):
    root = tmp_path / "repo"
    first_commit = commit(git, root, "tracked.py", "initial raw\r\n", "initial")
    (root / "a.custom").write_bytes(b"raw\r\n")
    (root / "gone.txt").write_bytes(b"delete later\n")
    projects, first = register(db, root)
    finish(db, first)
    pid = str(db.detail(first)["project_id"])
    original = db.detail(first)
    first_rows = inventory(db, first)
    assert original["summary"]["comparison"] == {
        "available": False,
        "baseline_id": None,
        "reason": "first_observation",
    }
    before_storage = storage_counts(db)
    second = observe(db, projects, pid)
    second_rows = inventory(db, str(second["id"]))
    assert storage_counts(db) == before_storage
    assert set(second_rows) == set(first_rows)
    for key, old in first_rows.items():
        assert second_rows[key]["content_id"] == old["content_id"]
        assert second_rows[key]["id"] != old["id"]
        assert second_rows[key]["observed_at"] > old["observed_at"]
    assert second["summary"]["comparison"] == {
        "available": True,
        "baseline_id": first,
        "unit": "layer_path_stage_observation",
        "new": 0,
        "changed": 0,
        "unchanged": 4,
        "deleted": 0,
        "unknown": 0,
    }
    assert bytes(second_rows[("untracked", "a.custom", 0)]["body"]) == b"raw\r\n"
    assert (
        second_rows[("untracked", "a.custom", 0)]["sha256"]
        == hashlib.sha256(b"raw\r\n").hexdigest()
    )
    (root / "a.custom").write_bytes(b"changed\r\n")
    (root / "gone.txt").unlink()
    (root / "new.txt").write_bytes(b"new text\n")
    (root / "tracked.py").write_bytes(b"working change\n")
    third = observe(db, projects, pid)
    comparison = third["summary"]["comparison"]
    assert {k: comparison[k] for k in ("new", "changed", "unchanged", "deleted", "unknown")} == {
        "new": 1,
        "changed": 2,
        "unchanged": 1,
        "deleted": 1,
        "unknown": 0,
    }
    third_rows = inventory(db, str(third["id"]))
    assert ("untracked", "gone.txt", 0) not in third_rows
    assert bytes(third_rows[("working", "tracked.py", 0)]["body"]) == b"working change\n"
    assert (
        third_rows[("untracked", "a.custom", 0)]["content_id"]
        != first_rows[("untracked", "a.custom", 0)]["content_id"]
    )
    git("add", "tracked.py")
    git("commit", "-m", "new commit")
    latest = git("rev-parse", "HEAD").decode()
    fourth = observe(db, projects, pid)
    assert {
        r["oid"] for r in db.all("SELECT oid FROM commits WHERE collection_id=%s", (fourth["id"],))
    } == {first_commit, latest}
    assert db.one("SELECT count(*) AS n FROM git_commits")["n"] == 2
    old_pointer = db.one(
        "SELECT content_id FROM commits WHERE collection_id=%s AND oid=%s", (first, first_commit)
    )
    assert (
        db.one(
            "SELECT content_id FROM commits WHERE collection_id=%s AND oid=%s",
            (fourth["id"], first_commit),
        )
        == old_pointer
    )
    assert db.detail(first) == original
    assert inventory(db, first) == first_rows
    assert not db.one("SELECT count(*) AS n FROM working_entries WHERE body IS NOT NULL")["n"]
    with TestClient(create_app(db, start_worker=False)) as client:
        row = third_rows[("untracked", "a.custom", 0)]
        response = client.get(f"/api/collections/{third['id']}/content/working_entries/{row['id']}")
        assert response.json()["body"] == "changed\r\n"
        assert (
            client.get(f"/api/collections/{first}/content/working_entries/{row['id']}").status_code
            == 404
        )


def test_branch_reset_reobserves_all_current_refs_not_head_delta(db, git, tmp_path):
    root = tmp_path / "repo"
    first_oid = commit(git, root, "a", "first\n", "first")
    projects, cid = register(db, root)
    finish(db, cid)
    pid = str(db.detail(cid)["project_id"])
    git("checkout", "-b", "side")
    side = commit(git, root, "b", "side\n", "side")
    projects.update(pid, Settings(name="Test", status="ongoing", base_branch="side"))
    second = observe(db, projects, pid)
    assert second["snapshot"]["branch"] == "side"
    git("checkout", "main")
    projects.update(pid, Settings(name="Test", status="ongoing", base_branch="main"))
    git("branch", "-D", "side")
    third = observe(db, projects, pid)
    assert third["snapshot"]["head"] == first_oid
    assert third["snapshot"]["branch"] == "main"
    assert {
        r["oid"] for r in db.all("SELECT oid FROM commits WHERE collection_id=%s", (third["id"],))
    } == {first_oid}
    assert side in {
        r["oid"] for r in db.all("SELECT oid FROM commits WHERE collection_id=%s", (second["id"],))
    }


def test_safe_untracked_all_text_configs_secrets_and_read_failure(db, git, tmp_path):
    root = tmp_path / "repo"
    bodies = {
        "application.yml": b"server:\n  port: 8080\n",
        "application.properties": b"server.port=8080\n",
        "unknown.extension": b"general text\n",
        "bad.yml": b"password: syntheticPrivate123456\n",
        "hardcoded.py": b'api_key = "syntheticSecret123456789"\n',
        ".env": b"sensitive even without a recognizable credential\n",
        "binary": b"a\0b",
        "encoding": b"\xff\xfe",
        "large": b"x" * (MAX_BODY + 1),
    }
    for path, raw in bodies.items():
        (root / path).write_bytes(raw)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored/private.txt").write_text("must not recurse\n")
    (tmp_path / "outside").write_text("must not follow\n")
    (root / "link").symlink_to(tmp_path / "outside")
    unreadable = root / "unreadable.txt"
    unreadable.write_text("not readable\n")
    unreadable.chmod(0)
    try:
        _, cid = register(db, root)
        collect(db, cid, lambda: False)
    finally:
        unreadable.chmod(0o600)
    rows = inventory(db, cid)
    for path in ("application.yml", "application.properties", "unknown.extension"):
        row = rows[("untracked", path, 0)]
        assert bytes(row["body"]) == bodies[path]
        assert row["sha256"] == hashlib.sha256(bodies[path]).hexdigest()
    expected = {
        "bad.yml": "suspected_secret",
        "hardcoded.py": "suspected_secret",
        ".env": "sensitive_path",
        "binary": "binary",
        "encoding": "non_utf8",
        "large": "large",
        "link": "symlink",
        "unreadable.txt": "unreadable_or_symlink_parent",
    }
    for path, reason in expected.items():
        row = rows[("untracked", path, 0)]
        assert row["body_reason"] == reason
        assert row["body"] is None and row["sha256"] is None and row["content_id"] is None
    assert not any(key[1].endswith("private.txt") for key in rows)
    assert rows[("ignored", "ignored/", 0)]["body"] is None
    result = db.detail(cid)
    assert result["state"] == "cleanup_pending"
    assert result["summary"]["errors"] == 1
    assert {r["reason"] for r in result["summary"]["body_errors"]} == {
        "unreadable_or_symlink_parent"
    }
    assert "suspected_secret" in {r["reason"] for r in result["summary"]["body_exclusions"]}
    stored = [bytes(r["body"]) for r in db.all("SELECT body FROM contents")]
    assert bodies["bad.yml"] not in stored and bodies["hardcoded.py"] not in stored


def test_commitless_staged_working_untracked_and_untracked_rename(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "staged.txt").write_bytes(b"index\n")
    git("add", "staged.txt")
    (root / "staged.txt").write_bytes(b"working\n")
    (root / "old.txt").write_bytes(b"untracked\n")
    projects, cid = register(db, root)
    finish(db, cid)
    rows = inventory(db, cid)
    assert bytes(rows[("index", "staged.txt", 0)]["body"]) == b"index\n"
    assert bytes(rows[("working", "staged.txt", 0)]["body"]) == b"working\n"
    assert db.detail(cid)["snapshot"]["head"] is None
    (root / "old.txt").rename(root / "renamed.txt")
    second = observe(db, projects, str(db.detail(cid)["project_id"]))
    row = inventory(db, str(second["id"]))[("untracked", "renamed.txt", 0)]
    assert row["metadata"]["status"] == "??" and "previous" not in row["metadata"]
    assert second["summary"]["comparison"]["new"] == 1
    assert second["summary"]["comparison"]["deleted"] == 1
    assert second["summary"]["commits"] == 0


@pytest.mark.parametrize("state", ["capturing", "queued", "running"])
def test_active_slot_rejects_manual_and_has_no_retry_endpoint(db, git, tmp_path, state):
    projects, old = register(db, tmp_path / "repo")
    finish(db, old)
    pid = str(db.detail(old)["project_id"])
    active = projects.collect_now(pid)
    db.execute("UPDATE collections SET state=%s WHERE id=%s", (state, active["id"]))
    with TestClient(create_app(db, start_worker=False)) as client:
        assert client.post(f"/api/projects/{pid}/collections", json={}).status_code == 409
        assert client.post(f"/api/collections/{old}/retry", json={}).status_code in {404, 405}
    assert db.one("SELECT count(*) AS n FROM collections WHERE project_id=%s", (pid,))["n"] == 2


@pytest.mark.parametrize("actions", [("manual", "manual")])
def test_concurrent_manual_requests_share_one_active_slot(db, git, tmp_path, actions):
    projects, old = register(db, tmp_path / "repo")
    finish(db, old)
    pid = str(db.detail(old)["project_id"])
    barrier = threading.Barrier(2)

    def start(action):
        barrier.wait(timeout=10)
        try:
            return projects.collect_now(pid)
        except CollectionError as exc:
            assert exc.code == "conflict"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, action) for action in actions]
        results = [f.result(timeout=20) for f in futures]
    assert sum(r is not None for r in results) == 1
    assert (
        db.one(
            "SELECT count(*) AS n FROM collections WHERE state IN ('capturing','queued','running')"
        )["n"]
        == 1
    )


def test_manual_keeps_completed_old_bytes_and_gets_current_bytes(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "a.txt").write_text("original observation\n")
    projects, old = register(db, root)
    finish(db, old)
    pid = str(db.detail(old)["project_id"])
    original = inventory(db, old)
    (root / "a.txt").write_text("current observation\n")
    manual = projects.collect_now(pid)
    finish(db, str(manual["id"]))
    assert inventory(db, old) == original
    current = inventory(db, str(manual["id"]))
    assert bytes(current[("untracked", "a.txt", 0)]["body"]) == b"current observation\n"
    assert manual["kind"] == "manual"


def test_collection_target_files_index_head_refs_unchanged(db, git, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    from scripts.verify_repository import fingerprint

    root = tmp_path / "repo"
    commit(git, root, "tracked", "base\n", "base")
    (root / "tracked").write_text("dirty\n")
    (root / "new").write_text("safe untracked\n")
    before = fingerprint(root)
    projects, cid = register(db, root)
    finish(db, cid)
    assert fingerprint(root) == before
    observe(db, projects, str(db.detail(cid)["project_id"]))
    assert fingerprint(root) == before


def test_upgrade_001_preserves_records_bodies_times_and_is_idempotent():
    project, collection = str(uuid4()), str(uuid4())
    with temporary_database() as dsn:
        db = Database(dsn)
        initial = Path("src/project_log/migrations/001_initial.sql").read_bytes()
        with db.connect() as conn:
            conn.execute(initial.decode())
            conn.execute(
                """CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            conn.execute(
                "INSERT INTO schema_migrations(version,sha256) VALUES ('001_initial.sql',%s)",
                (hashlib.sha256(initial).hexdigest(),),
            )
            conn.execute(
                """INSERT INTO projects
                (id,name,path,repository_key,status,coding_agent,repository_info)
                VALUES (%s,'old','/synthetic','/synthetic/.git','ongoing','none','{}')""",
                (project,),
            )
            conn.execute(
                """INSERT INTO collections(id,project_id,state,snapshot,policy,summary)
                VALUES (%s,%s,'completed',%s,'{}','{}')""",
                (collection, project, Jsonb({"complete": True})),
            )
            meta = {
                "path": "old.txt",
                "path_b64": base64.b64encode(b"old.txt").decode(),
                "status": " M",
            }
            conn.execute(
                """INSERT INTO working_entries
                (collection_id,layer,metadata,body,sha256,observed_at)
                VALUES (%s,'working',%s,%s,%s,now()),(%s,'index',%s,%s,%s,now())""",
                (
                    collection,
                    Jsonb(meta),
                    b"old raw\r\n",
                    hashlib.sha256(b"old raw\r\n").hexdigest(),
                    collection,
                    Jsonb(meta),
                    b"old raw\r\n",
                    hashlib.sha256(b"old raw\r\n").hexdigest(),
                ),
            )
            oid = "a" * 40
            conn.execute(
                "INSERT INTO commits(collection_id,oid,metadata,raw) VALUES (%s,%s,%s,%s)",
                (
                    collection,
                    oid,
                    Jsonb({"parents": [], "message": "old\n"}),
                    b"old commit\n",
                ),
            )
            change = {"old": meta, "new": meta}
            conn.execute(
                "INSERT INTO changes(collection_id,commit_oid,metadata,diff) VALUES (%s,%s,%s,%s)",
                (collection, oid, Jsonb(change), b"old diff\n"),
            )
            conn.execute(
                "INSERT INTO head_files(collection_id,metadata) VALUES (%s,%s)",
                (collection, Jsonb({**meta, "commit": oid, "oid": "b" * 40})),
            )
        before = {
            table: db.all(f"SELECT * FROM {table}")
            for table in ("collections", "working_entries", "commits", "changes", "head_files")
        }
        db.migrate()
        ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
        db.migrate()
        assert db.all("SELECT * FROM schema_migrations ORDER BY version") == ledger
        assert len(ledger) == 6
        assert db.one("SELECT base_branch FROM projects")["base_branch"] is None
        after = {
            "working_entries": "working_records",
            "commits": "commit_records",
            "changes": "change_records",
            "head_files": "head_records",
        }
        for original_table, view in after.items():
            old_rows, new_rows = before[original_table], db.all(f"SELECT * FROM {view}")
            assert len(old_rows) == len(new_rows)
            for old, new in zip(old_rows, new_rows, strict=True):
                assert {k: new[k] for k in old} == old
        original_collection = before["collections"][0]
        migrated = db.one("SELECT * FROM collections")
        assert {k: migrated[k] for k in original_collection} == original_collection
        assert db.one("SELECT count(*) AS n FROM contents")["n"] == 3
        assert not db.one("SELECT count(*) AS n FROM working_entries WHERE body IS NOT NULL")["n"]
        assert (
            db.one("SELECT count(*) AS n FROM pg_constraint WHERE conname='one_active_collection'")[
                "n"
            ]
            == 0
        )
        index = db.one("SELECT indexdef FROM pg_indexes WHERE indexname='one_active_collection'")
        assert all(state in index["indexdef"] for state in ("capturing", "queued", "running"))
        assert (
            db.one(
                "SELECT count(*) AS n FROM information_schema.views WHERE table_schema='public'"
            )["n"]
            == 4
        )
        with db.connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO contents(project_id,sha256,body) VALUES (%s,'wrong',%s)",
                (project, b"raw"),
            )


@pytest.mark.parametrize("limit", ["bytes", "seconds"])
def test_untracked_snapshot_limit_keeps_metadata_and_explicit_failure(
    db, git, tmp_path, monkeypatch, limit
):
    import project_log.collector as collector

    root = tmp_path / "repo"
    (root / "candidate.data").write_bytes(b"safe general text\r\n")
    monkeypatch.setattr(collector, "SNAPSHOT_BYTES" if limit == "bytes" else "SNAPSHOT_SECONDS", 0)
    _, cid = register(db, root)
    collect(db, cid, lambda: False)
    result = db.detail(cid)
    row = db.one("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert row["layer"] == "untracked" and row["metadata"]["path"] == "candidate.data"
    assert row["body_reason"] == "snapshot_budget"
    assert row["body"] is None and row["sha256"] is None and row["content_id"] is None
    assert result["state"] == "cleanup_pending" and not result["snapshot"]["complete"]
    assert result["summary"]["body_errors"] == [{"reason": "snapshot_budget", "count": 1}]
    assert result["summary"]["body_exclusions"] == []


def test_migration_checksum_mismatch_is_rejected_without_ledger_or_data_changes(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "untracked.txt").write_text("preserve\n")
    _, cid = register(db, root)
    finish(db, cid)
    before = db.all("SELECT * FROM working_records ORDER BY id")
    db.execute(
        "UPDATE schema_migrations SET sha256='synthetic-mismatch' WHERE version=%s",
        ("002_collection_content.sql",),
    )
    ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        db.migrate()
    assert db.all("SELECT * FROM schema_migrations ORDER BY version") == ledger
    assert db.all("SELECT * FROM working_records ORDER BY id") == before


def test_database_rejects_cross_project_content_and_second_active_slot(db, git, tmp_path):
    from uuid import uuid4

    root = tmp_path / "repo"
    (root / "a.txt").write_text("local Evidence\n")
    _, cid = register(db, root)
    finish(db, cid)
    local = db.one("SELECT * FROM working_entries WHERE collection_id=%s", (cid,))
    other = str(uuid4())
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO projects
            (id,name,path,repository_key,status,coding_agent,repository_info)
            VALUES (%s,'other','/synthetic-other','/synthetic-other/.git','new','none','{}')""",
            (other,),
        )
        content = conn.execute(
            """INSERT INTO contents(project_id,sha256,body)
            VALUES (%s,%s,%s) RETURNING id""",
            (other, hashlib.sha256(b"other Evidence\n").hexdigest(), b"other Evidence\n"),
        ).fetchone()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "UPDATE working_entries SET content_id=%s WHERE id=%s", (content["id"], local["id"])
        )
    assert (
        db.one("SELECT content_id FROM working_entries WHERE id=%s", (local["id"],))["content_id"]
        == local["content_id"]
    )
    db.execute("UPDATE collections SET state='queued' WHERE id=%s", (cid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO collections(id,project_id,state,policy) VALUES (%s,%s,'capturing','{}')",
            (str(uuid4()), local["project_id"]),
        )
    assert db.one("SELECT count(*) AS n FROM collections")["n"] == 1
