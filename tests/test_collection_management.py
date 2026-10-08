import hashlib
import uuid
from pathlib import Path

import pytest

from project_log.collections import Collections, Memo
from project_log.collector import collect
from project_log.db import Database
from project_log.git import CollectionError
from project_log.materials import Materials
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


def test_memos_only_completed_duplicate_clear_and_evidence_identity_unchanged(db, git, tmp_path):
    projects, pid, first = completed(db, git, tmp_path)
    second = str(projects.collect_now(pid)["id"])
    management = Collections(db)
    with pytest.raises(CollectionError):
        management.memo(second, Memo(title="forbidden"))
    with pytest.raises(CollectionError):
        management.remove(second)
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
