import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from project_log import worker as worker_module
from project_log.collections import Collections, Memo
from project_log.collector import collect
from project_log.db import CollectionStopped, Database
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.worker import Worker
from scripts.verification_db import temporary_database
from tests.test_collection import commit, register

TABLES = (
    "projects",
    "collections",
    "collection_issues",
    "working_entries",
    "working_repairs",
    "collection_repairs",
    "commits",
    "changes",
    "head_files",
    "contents",
    "git_commits",
    "git_changes",
    "git_files",
    "user_materials",
)


def snapshot(db):
    return {
        table: db.all(f"SELECT to_jsonb(t) AS data FROM {table} t ORDER BY to_jsonb(t)::text")
        for table in TABLES
    }


def completed(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "a.txt", "shared\n", "first")
    projects, first = register(db, root)
    collect(db, first, lambda: False)
    return projects, str(db.detail(first)["project_id"]), first


def fail_removal(db):
    db.execute("""CREATE FUNCTION reject_removal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic transaction failure'; END $$;
        CREATE TRIGGER reject_removal BEFORE DELETE ON contents
        FOR EACH ROW EXECUTE FUNCTION reject_removal()""")


def assert_gone(db, cid):
    assert not db.all("SELECT id FROM collections WHERE id=%s", (cid,))
    for table in (
        "working_entries",
        "commits",
        "changes",
        "head_files",
        "collection_issues",
        "collection_repairs",
    ):
        assert not db.all(f"SELECT 1 FROM {table} WHERE collection_id=%s", (cid,))


@pytest.mark.parametrize("state", ["capturing", "queued", "running"])
def test_cancel_committed_intent_gates_every_writer_and_active_slot(db, git, tmp_path, state):
    projects, cid = register(db, tmp_path / "repo")
    pid = str(db.detail(cid)["project_id"])
    db.execute("UPDATE collections SET state=%s WHERE id=%s", (state, cid))
    assert Collections(db).cancel(cid)["state"] == "cancel_pending"
    before = snapshot(db)
    with pytest.raises(CollectionStopped):
        db.issue(cid, "worker", "forbidden", "must not write")
    with pytest.raises(CollectionStopped):
        db.summarize(cid)
    with pytest.raises(CollectionStopped):
        db.observe_commit(cid, pid, "a" * 40, {"parents": []}, b"forbidden", None)
    with pytest.raises(CollectionStopped):
        db.observe_change(cid, pid, "a" * 40, None, {}, b"forbidden", None)
    with pytest.raises(CollectionStopped):
        db.observe_head(cid, pid, {})
    with pytest.raises(CollectionError, match="진행 중"):
        projects.collect_now(pid)
    with pytest.raises(CollectionError):
        Collections(db).cancel(cid)
    db.mark_cleanup(cid)
    assert snapshot(db) == before  # committed cancel never becomes generic failure
    worker = Worker(db)
    try:
        assert worker.once()
    finally:
        worker.close()
    assert_gone(db, cid)
    new = projects.collect_now(pid)
    collect(db, str(new["id"]), lambda: False)
    assert db.detail(str(new["id"]))["state"] == "completed"


def test_cancel_waits_for_inflight_transaction_then_blocks_later_worker_writes(
    db, git, tmp_path, monkeypatch
):
    projects, first = register(db, tmp_path / "repo")
    pid = str(db.detail(first)["project_id"])
    entered, release = threading.Event(), threading.Event()
    rejected = []

    def controlled(db, cid, stopped):
        with db.writer(cid) as conn:
            conn.execute("UPDATE collections SET summary='{}' WHERE id=%s", (cid,))
            entered.set()
            assert release.wait(10)
        # Wait until the cancellation transaction actually commits.
        assert cancelled.wait(10)
        try:
            db.issue(cid, "worker", "late", "late write")
        except CollectionStopped:
            rejected.append(True)
            raise

    cancelled = threading.Event()
    monkeypatch.setattr(worker_module, "collect", controlled)
    worker = Worker(db)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(worker.once)
            assert entered.wait(10)
            cancelling = pool.submit(Collections(db).cancel, first)
            assert not cancelling.done()
            release.set()
            assert cancelling.result(timeout=10)["state"] == "cancel_pending"
            cancelled.set()
            assert running.result(timeout=10)
        assert rejected == [True]
        assert_gone(db, first)
        assert projects.project(pid)["collections"] == []
    finally:
        release.set()
        cancelled.set()
        worker.close()


@pytest.mark.parametrize("mode", ["delete", "cancel", "failure"])
def test_atomic_removal_rollback_and_restart_cleanup_only(db, git, tmp_path, monkeypatch, mode):
    projects, pid, cid = completed(db, git, tmp_path)
    if mode != "delete":
        db.execute("UPDATE collections SET state='running' WHERE id=%s", (cid,))
        if mode == "cancel":
            Collections(db).cancel(cid)
        else:
            db.mark_cleanup(cid)
    expected = "cancel_pending" if mode == "cancel" else "cleanup_pending"
    before = snapshot(db)
    fail_removal(db)
    with pytest.raises(psycopg.Error, match="synthetic"):
        Collections(db).remove(cid, cleanup=mode != "delete")
    assert snapshot(db) == before
    if mode == "delete":
        assert db.detail(cid)["state"] == "completed"
        db.execute("DROP TRIGGER reject_removal ON contents")
        Collections(db).remove(cid)
    else:
        assert db.detail(cid)["state"] == expected
        with TestClient(
            __import__("project_log.api", fromlist=["create_app"]).create_app(
                db, start_worker=False
            )
        ) as client:
            detail = client.get(f"/api/projects/{pid}").json()
            assert detail["collection_busy"]
            if mode == "failure":
                assert detail["collections"] == []
                for suffix in ("", "/overview", "/content/working_entries/1", "/commits"):
                    assert client.get(f"/api/collections/{cid}{suffix}").status_code == 404
            assert client.post(f"/api/projects/{pid}/collections", json={}).status_code == 409
            assert client.post(f"/api/collections/{cid}/retry", json={}).status_code in {404, 405}
        # A second process lifecycle with the same persisted failed cleanup must
        # leave the intent intact, and must never call collect (no Resume).
        for _ in range(2):
            worker = Worker(db)
            monkeypatch.setattr(worker.thread, "start", lambda: None)
            monkeypatch.setattr(
                worker_module, "collect", lambda *a: pytest.fail("resumed collection")
            )
            try:
                worker.start()
                assert db.detail(cid)["state"] == expected
                assert not worker.once()
                assert worker.error is not None
                assert snapshot(db) == before
            finally:
                worker.close()
        db.execute("DROP TRIGGER reject_removal ON contents")
        worker = Worker(db)
        try:
            assert worker.once()
        finally:
            worker.close()
    assert_gone(db, cid)
    assert projects.project(pid)["collections"] == []
    assert db.one("SELECT base_branch FROM projects WHERE id=%s", (pid,))["base_branch"] == "main"


@pytest.mark.parametrize("state", ["capturing", "queued", "running"])
def test_restart_orphan_active_is_cleaned_never_resumed(db, git, tmp_path, monkeypatch, state):
    projects, cid = register(db, tmp_path / "repo")
    db.execute("UPDATE collections SET state=%s WHERE id=%s", (state, cid))
    worker = Worker(db)
    monkeypatch.setattr(worker.thread, "start", lambda: None)
    monkeypatch.setattr(worker_module, "collect", lambda *a: pytest.fail("orphan resumed"))
    try:
        worker.start()
        assert db.detail(cid)["state"] == "cleanup_pending"
        worker.once()
    finally:
        worker.close()
    assert_gone(db, cid)
    assert db.one("SELECT count(*) AS n FROM projects")["n"] == 1


def test_general_worker_error_partial_writes_removed_and_new_manual_starts_fresh(
    db, git, tmp_path, monkeypatch
):
    projects, pid, first = completed(db, git, tmp_path)
    (tmp_path / "repo/a.txt").write_text("new attempt\n")
    cid = str(projects.collect_now(pid)["id"])
    old = snapshot(db)
    original = worker_module.collect

    def fail(db, cid, stopped):
        db.observe_commit(cid, pid, "a" * 40, {"parents": []}, b"partial message", None)
        raise RuntimeError("synthetic failure")

    worker = Worker(db)
    try:
        monkeypatch.setattr(worker_module, "collect", fail)
        assert worker.once()
        assert_gone(db, cid)
        assert not db.all("SELECT 1 FROM git_commits WHERE oid=%s", ("a" * 40,))
        assert db.detail(first)["state"] == "completed"
        monkeypatch.setattr(worker_module, "collect", original)
        new = projects.collect_now(pid)
        assert str(new["id"]) != cid
        assert worker.once()
        assert db.detail(str(new["id"]))["state"] == "completed"
    finally:
        worker.close()
    assert old["projects"] == snapshot(db)["projects"]


@pytest.mark.parametrize("mode", ["delete", "cancel", "failure"])
def test_delete_preserves_shared_data_other_collections_material_and_repository(
    db, git, tmp_path, mode
):
    projects, pid, first = completed(db, git, tmp_path)
    root = tmp_path / "repo"
    material = Materials(db).add(pid, "memo.txt", b"project owned material")
    original_project = snapshot(db)["projects"]
    (root / "only.txt").write_text("collection-only bytes\n")
    commit(git, root, "unique.txt", "unique committed\n", "unique commit")
    second = str(projects.collect_now(pid)["id"])
    collect(db, second, lambda: False)
    protected = {
        t: db.all(
            f"SELECT to_jsonb(t) AS data FROM {t} t "
            f"WHERE {'id' if t == 'collections' else 'collection_id'}=%s",
            (first,),
        )
        for t in ("collections", "commits", "changes", "working_entries", "head_files")
    }
    repo_before = (
        git("show-ref"),
        git("status", "--porcelain"),
        (root / ".git/index").read_bytes(),
    )
    if mode != "delete":
        db.execute("UPDATE collections SET state='running' WHERE id=%s", (second,))
        if mode == "cancel":
            Collections(db).cancel(second)
        else:
            db.mark_cleanup(second)
    Collections(db).remove(second, cleanup=mode != "delete")
    assert_gone(db, second)
    assert not db.all("SELECT id FROM contents WHERE body=%s", (b"collection-only bytes\n",))
    for t, rows in protected.items():
        key = "id" if t == "collections" else "collection_id"
        assert db.all(f"SELECT to_jsonb(t) AS data FROM {t} t WHERE {key}=%s", (first,)) == rows
    assert original_project == snapshot(db)["projects"]
    assert Materials(db).read(pid, str(material["id"]))[1] == b"project owned material"
    assert repo_before == (
        git("show-ref"),
        git("status", "--porcelain"),
        (root / ".git/index").read_bytes(),
    )
    Collections(db).remove(first)
    assert projects.project(pid)["collections"] == []
    for table in ("contents", "git_commits", "git_changes", "git_files"):
        assert db.one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
    assert Materials(db).read(pid, str(material["id"]))[1] == b"project owned material"


def test_delete_baseline_and_legacy_repair_references(db, git, tmp_path):
    projects, pid, first = completed(db, git, tmp_path)
    second = str(projects.collect_now(pid)["id"])
    collect(db, second, lambda: False)
    w = db.one("SELECT * FROM working_entries WHERE collection_id=%s LIMIT 1", (first,))
    db.execute(
        """INSERT INTO working_repairs
        (working_entry_id,project_id,content_id,body_observed_at,read_metadata,provenance)
        VALUES (%s,%s,%s,now(),'{}','{}')""",
        (w["id"], pid, w["content_id"]),
    )
    db.execute(
        """INSERT INTO collection_repairs(collection_id,project_id,evidence_sha256,original_summary)
        VALUES (%s,%s,'synthetic','{}')""",
        (first, pid),
    )
    db.execute("UPDATE collections SET retry_of=%s WHERE id=%s", (first, second))
    Collections(db).remove(first)
    row = db.detail(second)
    assert row["baseline_id"] is None and row["retry_of"] is None
    assert row["summary"]["comparison"]["reason"] == "baseline_deleted"
    assert not db.all("SELECT * FROM working_repairs")
    assert not db.all("SELECT * FROM collection_repairs")


def test_memos_active_and_completed_duplicate_clear_and_evidence_identity_unchanged(
    db, git, tmp_path
):
    projects, pid, first = completed(db, git, tmp_path)
    second = str(projects.collect_now(pid)["id"])
    management = Collections(db)
    management.memo(second, Memo(title="수집 중 메모", description="선택 입력"))
    assert db.detail(second)["state"] == "queued"
    with pytest.raises(CollectionError):
        management.remove(second)
    with pytest.raises(CollectionError):
        management.cancel(first)
    collect(db, second, lambda: False)
    before = snapshot(db)
    for cid in (first, second):
        row = management.memo(cid, Memo(title="same", description="memo\nonly"))
        assert row["title"] == "same" and row["description"] == "memo\nonly"
    management.memo(first, Memo(title="changed", description="changed"))
    management.memo(first, Memo(title="", description=""))
    assert db.detail(first)["title"] is None and db.detail(first)["description"] is None
    after = snapshot(db)
    for rows in (before["collections"], after["collections"]):
        for r in rows:
            r["data"].pop("title")
            r["data"].pop("description")
    assert before == after


def test_005_additive_upgrade_preserves_all_existing_rows_and_completed_lookup(git, tmp_path):
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for file in sorted(Path("src/project_log/migrations").glob("*.sql"))[:5]:
                raw = file.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (file.name, hashlib.sha256(raw).hexdigest()),
                )
        projects, pid, cid = completed(db, git, tmp_path)
        Materials(db).add(pid, "old.txt", b"old material")
        before = snapshot(db)
        ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
        db.migrate()
        db.migrate()
        after = snapshot(db)
        for r in after["collections"]:
            assert r["data"].pop("title") is None
            assert r["data"].pop("description") is None
        assert before == after
        assert db.all("SELECT * FROM schema_migrations ORDER BY version")[:5] == ledger
        assert projects.project(pid)["collections"][0]["id"] == uuid.UUID(cid)


@pytest.mark.parametrize("mode", ["delete", "cancel", "failure"])
def test_process_death_during_actual_removal_rolls_back_and_restarts_cleanup_only(
    db, git, tmp_path, monkeypatch, mode
):
    import subprocess
    import sys

    projects, pid, cid = completed(db, git, tmp_path)
    if mode != "delete":
        db.execute("UPDATE collections SET state='running' WHERE id=%s", (cid,))
        if mode == "cancel":
            Collections(db).cancel(cid)
        else:
            db.mark_cleanup(cid)
    before = snapshot(db)
    script = """
import sys, time
from contextlib import contextmanager
from project_log.db import Database
from project_log.collections import Collections
class Connection:
    def __init__(self, conn): self.conn = conn
    def execute(self, query, params=()):
        result = self.conn.execute(query, params)
        if query.startswith('DELETE FROM contents'):
            print('transaction not committed', flush=True)
            time.sleep(60)
        return result
class InterruptedDatabase(Database):
    @contextmanager
    def connect(self):
        with super().connect() as conn:
            yield Connection(conn)
Collections(InterruptedDatabase(sys.argv[1])).remove(sys.argv[2], cleanup=sys.argv[3]=='yes')
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, db.dsn, cid, "no" if mode == "delete" else "yes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "transaction not committed"
        assert snapshot(db) == before  # no partial changes visible while transaction is open
        child.terminate()  # only the owned synthetic verification subprocess
        child.wait(timeout=10)
        assert child.returncode != 0
        assert snapshot(db) == before
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    if mode == "delete":
        assert db.detail(cid)["state"] == "completed"
        Collections(db).remove(cid)
    else:
        worker = Worker(db)
        monkeypatch.setattr(worker.thread, "start", lambda: None)
        monkeypatch.setattr(worker_module, "collect", lambda *a: pytest.fail("resumed attempt"))
        try:
            worker.start()
            assert db.detail(cid)["state"] == (
                "cancel_pending" if mode == "cancel" else "cleanup_pending"
            )
            assert worker.once()
        finally:
            worker.close()
    assert_gone(db, cid)
    assert projects.project(pid)["collections"] == []


def test_cancel_transaction_failure_does_not_commit_intent(db, git, tmp_path):
    _, cid = register(db, tmp_path / "repo")
    before = snapshot(db)
    db.execute("""CREATE FUNCTION reject_cancel() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.state='cancel_pending' THEN RAISE EXCEPTION 'synthetic intent failure';
        END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_cancel BEFORE UPDATE ON collections
        FOR EACH ROW EXECUTE FUNCTION reject_cancel()""")
    with pytest.raises(psycopg.Error):
        Collections(db).cancel(cid)
    assert snapshot(db) == before
    db.issue(cid, "snapshot", "still_allowed", "intent was not committed")
    assert db.detail(cid)["state"] == "queued"


def test_cancel_while_synchronous_capture_reads_cannot_write_or_revive_deleted_attempt(
    db, git, tmp_path, monkeypatch
):
    from project_log.git import Git

    projects, pid, first = completed(db, git, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = Git.status

    def slow_status(self):
        entered.set()
        assert release.wait(10)
        return original(self)

    with monkeypatch.context() as patcher:
        patcher.setattr(Git, "status", slow_status)
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(projects.collect_now, pid)
            assert entered.wait(10)
            cid = str(db.one("SELECT id FROM collections WHERE state='capturing'")["id"])
            Collections(db).cancel(cid)
            Collections(db).remove(cid, cleanup=True)
            release.set()
            # The caller may have lost its attempt to cancellation; it cannot recreate it.
            assert attempt.result(timeout=10) is None
    assert_gone(db, cid)
    assert db.detail(first)["state"] == "completed"
    collect(db, str(projects.collect_now(pid)["id"]), lambda: False)


@pytest.mark.parametrize("mode", ["delete", "cancel", "failure"])
def test_all_removal_statements_share_one_postgresql_transaction(db, git, tmp_path, mode):
    _, _, cid = completed(db, git, tmp_path)
    db.execute("""CREATE TABLE deletion_transactions(table_name text, transaction_id bigint);
        CREATE FUNCTION audit_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO deletion_transactions VALUES (TG_TABLE_NAME,txid_current());
        RETURN OLD; END $$""")
    for table in (
        "collections",
        "working_entries",
        "commits",
        "changes",
        "head_files",
        "contents",
        "git_commits",
        "git_changes",
        "git_files",
    ):
        db.execute(
            f"CREATE TRIGGER audit_deletion AFTER DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_deletion()"
        )
    if mode != "delete":
        db.execute("UPDATE collections SET state='running' WHERE id=%s", (cid,))
        if mode == "cancel":
            Collections(db).cancel(cid)
        else:
            db.mark_cleanup(cid)
    Collections(db).remove(cid, cleanup=mode != "delete")
    rows = db.all("SELECT DISTINCT table_name,transaction_id FROM deletion_transactions")
    assert {r["table_name"] for r in rows} == {
        "collections",
        "working_entries",
        "commits",
        "changes",
        "head_files",
        "contents",
        "git_commits",
        "git_changes",
        "git_files",
    }
    assert len({r["transaction_id"] for r in rows}) == 1


def test_failed_cleanup_does_not_block_other_project_worker(db, git, tmp_path):
    import subprocess

    from project_log.service import Registration

    projects, pid, cid = completed(db, git, tmp_path)
    db.execute("UPDATE collections SET state='cleanup_pending' WHERE id=%s", (cid,))
    fail_removal(db)
    root = tmp_path / "other-repo"
    root.mkdir()
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    other = projects.register(
        Registration(path=str(root), name="Other", status="new", base_branch="main")
    )
    other_id = str(other["collections"][0]["id"])
    worker = Worker(db)
    try:
        assert worker.once()
        assert worker.error is not None
        assert db.detail(other_id)["state"] == "completed"
        assert db.detail(cid)["state"] == "cleanup_pending"
        with pytest.raises(CollectionError, match="진행 중"):
            projects.collect_now(pid)
    finally:
        worker.close()


def test_startup_cleans_multiple_legacy_failures_without_active_index_conflict(
    db, git, tmp_path, monkeypatch
):
    projects, pid, first = completed(db, git, tmp_path)
    second = str(projects.collect_now(pid)["id"])
    collect(db, second, lambda: False)
    queued = str(projects.collect_now(pid)["id"])
    db.execute("UPDATE collections SET state='failed' WHERE id=%s", (first,))
    db.execute("UPDATE collections SET state='partial' WHERE id=%s", (second,))
    worker = Worker(db)
    monkeypatch.setattr(worker.thread, "start", lambda: None)
    monkeypatch.setattr(worker_module, "collect", lambda *a: pytest.fail("legacy attempt resumed"))
    try:
        worker.start()
        assert db.detail(queued)["state"] == "cleanup_pending"
        assert projects.project(pid)["collections"] == []
        assert projects.project(pid)["collection_busy"]
        assert worker.once()
    finally:
        worker.close()
    for cid in (first, second, queued):
        assert_gone(db, cid)
    assert projects.project(pid)["collections"] == []
    assert not projects.project(pid)["collection_busy"]


def test_capture_error_manual_response_hides_pending_attempt_and_only_cleanup_retries(
    db, git, tmp_path, monkeypatch
):
    from project_log import service
    from project_log.api import create_app

    projects, pid, first = completed(db, git, tmp_path)
    original = service.capture

    def failed_capture(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic error after capture writes")

    with TestClient(create_app(db, start_worker=False)) as client:
        with monkeypatch.context() as patcher:
            patcher.setattr(service, "capture", failed_capture)
            response = client.post(f"/api/projects/{pid}/collections", json={})
        assert response.status_code == 202 and response.json() is None
        cid = str(db.one("SELECT id FROM collections WHERE state='cleanup_pending'")["id"])
        assert db.all("SELECT 1 FROM working_entries WHERE collection_id=%s", (cid,))
        detail = client.get(f"/api/projects/{pid}").json()
        assert [c["id"] for c in detail["collections"]] == [first]
        assert detail["collection_busy"]
        assert client.get(f"/api/collections/{cid}").status_code == 404
        assert client.post(f"/api/projects/{pid}/collections", json={}).status_code == 409
        worker = Worker(db)
        try:
            assert worker.once()
            assert_gone(db, cid)
            new = client.post(f"/api/projects/{pid}/collections", json={}).json()
            assert new["id"] != cid
            assert worker.once()
            assert db.detail(new["id"])["state"] == "completed"
        finally:
            worker.close()
    assert projects.project(pid)["collections"][-1]["id"] == uuid.UUID(first)
