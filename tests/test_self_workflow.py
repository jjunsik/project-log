from project_log import collector, self_workflow
from project_log.collections import Collections
from project_log.git import Git
from project_log.policy import path_info
from tests.test_collection import commit, register
from tests.test_finalization import finish


def assert_absent(db, pid):
    for surface in (
        "projects",
        "collections",
        "collection_issues",
        "working_entries",
        "working_repairs",
        "collection_repairs",
        "contents",
        "commits",
        "changes",
        "head_files",
        "git_commits",
        "git_changes",
        "git_files",
        "working_records",
        "change_records",
        "head_records",
        "commit_records",
    ):
        where = (
            "t.id=%s"
            if surface == "projects"
            else (
                "t.collection_id IN (SELECT id FROM collections WHERE project_id=%s)"
                if surface == "collection_issues"
                else "t.project_id=%s"
            )
        )
        rows = db.all(
            f"SELECT to_jsonb(t)-ARRAY['body','diff','raw'] AS data FROM {surface} t WHERE {where}",
            (pid,),
        )
        for row in rows:
            paths = self_workflow.metadata_paths(row["data"])
            assert not any(self_workflow.workflow_path(path) for path in paths), (surface, paths)


def test_self_collection_all_surfaces_and_native_mixed_changes(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "current_status.md", "move out\n", "mentions current_status.md and chatgpt/")
    commit(git, root, "chatgpt/tracked.md", "private synthetic\n", "private source")
    commit(git, root, "public.md", "move in\n", "public source")
    git("mv", "current_status.md", "retained.md")
    git("commit", "-m", "move out")
    out = git("rev-parse", "HEAD").decode()
    git("mv", "public.md", "chatgpt/from-public.md")
    git("commit", "-m", "move in")
    inward = git("rev-parse", "HEAD").decode()
    (root / ".gitignore").write_text("/docs/\n/chatgpt/\n/current_status.md\n/ordinary/\n")
    (root / "current_status.md").write_text("local handoff\n")
    (root / "chatgpt/private").mkdir()
    (root / "chatgpt/private/ignored.md").write_text("never read\n")
    (root / "ordinary").mkdir()
    (root / "ordinary/ignored.md").write_text("ordinary ignored\n")
    (root / "docs/decisions").mkdir(parents=True)
    (root / "docs/decisions/evidence.md").write_text("mentions current_status.md and chatgpt/\n")
    monkeypatch.setattr(self_workflow, "SELF_REPOSITORY_ROOT", root)
    original_read = collector.read_working
    original_blob = Git.blob
    original_run = Git.run

    def guarded_read(root, path):
        assert not self_workflow.workflow_path(path)
        return original_read(root, path)

    def guarded_blob(self, oid, path, mode="100644"):
        assert not self_workflow.workflow_path(path.encode())
        return original_blob(self, oid, path, mode)

    def guarded_run(self, *args, **kwargs):
        if "status" in args or "ls-files" in args or "diff-tree" in args:
            if "--patch" not in args:
                assert all(p in args for p in self_workflow.pathspecs(root))
        if "ls-tree" in args and "-r" in args:
            assert "--" in args
            assert b"chatgpt" not in args and b"current_status.md" not in args
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(collector, "read_working", guarded_read)
    monkeypatch.setattr(Git, "blob", guarded_blob)
    monkeypatch.setattr(Git, "run", guarded_run)
    before = (git("rev-parse", "HEAD"), git("show-ref"), (root / ".git/index").read_bytes())
    _, cid = register(db, root)
    row = finish(db, cid)
    pid = str(row["project_id"])
    assert_absent(db, pid)
    changes = db.all("SELECT * FROM change_records WHERE collection_id=%s", (cid,))
    kept = next(r for r in changes if r["commit_oid"] == out)
    removed = next(r for r in changes if r["commit_oid"] == inward)
    assert kept["metadata"]["status"] == "A"
    assert kept["metadata"]["old_oid"] is None
    assert kept["metadata"]["new"]["path"] == "retained.md"
    assert removed["metadata"]["status"] == "D"
    assert removed["metadata"]["new_oid"] is None
    assert removed["metadata"]["old"]["path"] == "public.md"
    assert b"move out" in bytes(kept["diff"])
    assert b"move in" in bytes(removed["diff"])
    working = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert any(
        r["metadata"]["path"] == "ordinary/" and r["body_reason"] == "ignored_metadata_only"
        for r in working
    )
    doc = next(r for r in working if r["metadata"]["path"] == "docs/decisions/evidence.md")
    assert doc["layer"] == "document" and bytes(doc["body"]).startswith(
        b"mentions current_status.md"
    )
    assert any(
        "current_status.md" in r["metadata"]["message"]
        for r in db.all("SELECT * FROM commit_records WHERE collection_id=%s", (cid,))
    )
    assert before == (git("rev-parse", "HEAD"), git("show-ref"), (root / ".git/index").read_bytes())


def test_self_exception_does_not_expand_file_path_to_descendants(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "current_status.md/keep.md", "non-target descendant\n", "directory")
    monkeypatch.setattr(self_workflow, "SELF_REPOSITORY_ROOT", root)
    _, cid = register(db, root)
    finish(db, cid)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert any(
        r["metadata"]["path"] == "current_status.md/keep.md" and r["body"] is not None for r in rows
    )
    assert (
        db.all("SELECT * FROM head_records WHERE collection_id=%s", (cid,))[0]["metadata"]["path"]
        == "current_status.md/keep.md"
    )


def test_other_repository_keeps_identical_names(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    commit(git, root, "chatgpt/tracked.md", "ordinary safe file\n", "ordinary")
    (root / ".gitignore").write_text("/current_status.md\n")
    (root / "current_status.md").write_text("ordinary ignored file\n")
    monkeypatch.setattr(self_workflow, "SELF_REPOSITORY_ROOT", tmp_path / "different")
    _, cid = register(db, root)
    finish(db, cid)
    rows = db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    assert any(
        r["metadata"]["path"] == "current_status.md" and r["layer"] == "ignored" for r in rows
    )
    assert any(
        r["metadata"]["path"] == "chatgpt/tracked.md" and r["body"] is not None for r in rows
    )
    assert any(
        r["metadata"]["path"] == "chatgpt/tracked.md"
        for r in db.all("SELECT * FROM head_records WHERE collection_id=%s", (cid,))
    )


def test_self_new_manual_does_not_reuse_removed_legacy_workflow_observations(
    db, git, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    (root / "current_status.md").write_text("legacy handoff\n")
    projects, cid = register(db, root)
    row = finish(db, cid)
    assert any(
        r["metadata"]["path"] == "current_status.md"
        for r in db.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,))
    )
    db.execute("UPDATE collections SET state='partial' WHERE id=%s", (cid,))
    db.execute("UPDATE collections SET state='running' WHERE id=%s", (cid,))
    db.issue(cid, "snapshot", "synthetic_target", "old target", path_info(b"chatgpt/private.md"))
    db.mark_cleanup(cid)
    Collections(db).remove(cid, cleanup=True)
    monkeypatch.setattr(self_workflow, "SELF_REPOSITORY_ROOT", root)
    retry = projects.collect_now(str(row["project_id"]))
    finish(db, str(retry["id"]))
    assert not db.all("SELECT id FROM collection_issues WHERE collection_id=%s", (retry["id"],))
    assert not db.all(
        """SELECT * FROM working_records WHERE collection_id=%s
        AND metadata->>'path'='current_status.md'""",
        (retry["id"],),
    )
    assert row["project_id"] == retry["project_id"]
