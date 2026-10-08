import hashlib
import os
import stat
from pathlib import Path

import pytest

from project_log import collector
from project_log.git import Git
from project_log.policy import MAX_BODY, path_info
from tests.test_collection import commit, register
from tests.test_finalization import finish, inventory


def repository_state(root: Path):
    git = Git(root)
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in names + [d for d in dirs if (Path(directory) / d).is_symlink()]:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            value = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(mode)
                else os.readlink(path)
                if stat.S_ISLNK(mode)
                else None
            )
            files[str(path.relative_to(root))] = (mode, value)
    index_file = root / ".git/index"
    index_bytes = (
        hashlib.sha256(index_file.read_bytes()).hexdigest() if index_file.exists() else None
    )
    return git.head(), git.refs(), git.index(), index_bytes, git.status(), files


def test_ignored_documents_safety_and_read_errors(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".gitignore").write_text("/docs/\n")
    (root / "docs").mkdir()
    bodies = {
        "safe": b"plain\n",
        "secret": b"password: syntheticPrivate123456\n",
        ".env": b"restricted path\n",
        "binary": b"a\0b",
        "encoding": b"\xff\xfe",
        "large": b"x" * (MAX_BODY + 1),
        "read-error": b"unreadable\n",
    }
    for name, body in bodies.items():
        (root / "docs" / name).write_bytes(body)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden").write_text("not followed\n")
    (root / "docs/link").symlink_to(outside, target_is_directory=True)
    os.mkfifo(root / "docs/fifo")
    (root / "docs/.ssh").mkdir()
    (root / "docs/.ssh/hidden").write_text("not traversed\n")
    original_read = collector.read_working

    def read(path, raw):
        return (
            (None, "unreadable_or_symlink_parent", {})
            if raw == b"docs/read-error"
            else original_read(path, raw)
        )

    monkeypatch.setattr(collector, "read_working", read)
    before = repository_state(root)
    _, cid = register(db, root)
    collector.collect(db, cid, lambda: False)
    assert repository_state(root) == before
    result = db.detail(cid)
    assert result["state"] == "cleanup_pending" and not result["snapshot"]["complete"]
    rows = inventory(db, cid)
    for name, reason in {
        "secret": "suspected_secret",
        ".env": "sensitive_path",
        "binary": "binary",
        "encoding": "non_utf8",
        "large": "large",
        "link": "symlink",
        "fifo": "non_regular",
        ".ssh": "sensitive_path",
        "read-error": "unreadable_or_symlink_parent",
    }.items():
        row = rows[("document", f"docs/{name}", 0)]
        assert row["body"] is None and row["sha256"] is None and row["content_id"] is None
        assert row["body_reason"] == reason
    assert not any(k[1].endswith("hidden") for k in rows)
    assert result["summary"]["body_errors"] == [
        {"reason": "unreadable_or_symlink_parent", "count": 1},
    ]
    assert "suspected_secret" in {r["reason"] for r in result["summary"]["body_exclusions"]}


@pytest.mark.parametrize("budget", ["bytes", "seconds"])
def test_document_snapshot_budget_is_failure_not_policy_exclusion(
    db, git, tmp_path, monkeypatch, budget
):
    root = tmp_path / "repo"
    (root / "docs").mkdir()
    (root / "docs/a").write_text("document text\n")
    git("config", "core.excludesFile", str(tmp_path / "ignore"))
    (tmp_path / "ignore").write_text("/docs/\n")
    monkeypatch.setattr(collector, "SNAPSHOT_BYTES" if budget == "bytes" else "SNAPSHOT_SECONDS", 0)
    _, cid = register(db, root)
    collector.collect(db, cid, lambda: False)
    row = inventory(db, cid)[("document", "docs/a", 0)]
    assert row["body"] is None and row["sha256"] is None
    assert row["body_reason"] == "snapshot_budget"
    assert db.detail(cid)["state"] == "cleanup_pending"
    assert db.detail(cid)["summary"]["body_exclusions"] == []


def test_ignored_document_changes_during_capture_are_visible(db, git, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".gitignore").write_text("/docs/\n")
    (root / "docs").mkdir()
    (root / "docs/a").write_text("original\n")
    original_read = collector.read_working

    def read(path, raw):
        value = original_read(path, raw)
        if raw == b"docs/a":
            (root / "docs/added-during-capture").write_text("concurrent addition\n")
        return value

    monkeypatch.setattr(collector, "read_working", read)
    _, cid = register(db, root)
    collector.collect(db, cid, lambda: False)
    result = db.detail(cid)
    assert result["state"] == "cleanup_pending" and not result["snapshot"]["complete"]
    assert "documents_changed_or_incomplete" in {r["code"] for r in result["issues"]}


def test_docs_root_symlink_is_not_followed(db, git, tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden").write_text("not followed\n")
    (root / "docs").symlink_to(outside, target_is_directory=True)
    _, cid = register(db, root)
    finish(db, cid)
    rows = inventory(db, cid)
    assert set(rows) == {("document", "docs", 0)}
    assert rows[("document", "docs", 0)]["body_reason"] == "symlink"


def test_document_integration_preserves_distinct_git_status_observations(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(git, root, "tracked", "original\n", "initial")
    git("rm", "--cached", "tracked")
    before = repository_state(root)
    _, cid = register(db, root)
    finish(db, cid)
    rows = inventory(db, cid)
    assert set(rows) == {("working", "tracked", 0), ("untracked", "tracked", 0)}
    assert {r["metadata"]["status"] for r in rows.values()} == {"D ", "??"}
    assert repository_state(root) == before


def test_multimodule_root_docs_exception_keeps_general_module_evidence(db, git, tmp_path):
    root = tmp_path / "repo"
    commit(
        git, root, "modules/ignored/docs/tracked.md", "tracked ignored module docs\n", "module a"
    )
    commit(git, root, "modules/active/docs/tracked.md", "tracked module docs\n", "module b")
    (root / ".gitignore").write_text("/docs/\n/modules/ignored/docs/\n/vendor/\n")
    bodies = {
        "docs/decisions/root.custom": b"root document\r\n",
        "modules/active/docs/nested/new.md": b"ordinary untracked module document\n",
        "modules/ignored/docs/decisions/hidden.md": b"ignored module document not read\n",
        "vendor/component/docs/hidden.md": b"ordinary ignored directory not traversed\n",
    }
    for name, body in bodies.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(body)
    before = repository_state(root)
    _, cid = register(db, root)
    result = finish(db, cid)
    assert repository_state(root) == before
    assert result["snapshot"]["document_roots"] == [path_info(b"docs")]
    rows = inventory(db, cid)
    assert {k for k in rows if k[0] == "document"} == {
        ("document", "docs/decisions/root.custom", 0),
    }
    for name in ("modules/ignored/docs/tracked.md", "modules/active/docs/tracked.md"):
        for layer in ("index", "working"):
            row = rows[(layer, name, 0)]
            assert row["body"] is not None
            assert "document_root" not in row["metadata"]
    name = "modules/active/docs/nested/new.md"
    untracked = rows[("untracked", name, 0)]
    assert bytes(untracked["body"]) == bodies[name]
    assert untracked["sha256"] == hashlib.sha256(bodies[name]).hexdigest()
    assert "document_root" not in untracked["metadata"]
    ignored = [row for key, row in rows.items() if key[0] == "ignored"]
    assert any(row["metadata"]["path"].startswith("modules/ignored/docs/") for row in ignored)
    assert all(row["body"] is None and row["sha256"] is None for row in ignored)
    assert not any(key[1].endswith("hidden.md") for key in rows)
    assert rows[("ignored", "vendor/", 0)]["body_reason"] == "ignored_metadata_only"
    assert result["summary"]["preserved_document_bodies"] == 1
