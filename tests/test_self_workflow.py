from project_log import self_workflow
from project_log.collections import Collections
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
