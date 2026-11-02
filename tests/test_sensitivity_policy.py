"""User-reviewed data and independent system boundaries, using synthetic data only."""

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from test_analysis_admission import fixture, prepared, submission
from test_collection import commit, register
from test_finalization import finish

from project_log import collector, self_workflow
from project_log.analysis_api import configured_profile, public_profile
from project_log.analysis_checkpoints import compatibility_policy
from project_log.analysis_snapshot import decode_snapshot, encode_snapshot
from project_log.experience_input import ExperienceInputs
from project_log.gemini import read_operator_key
from project_log.git import CollectionError, Git, read_working
from project_log.materials import Materials
from project_log.policy import MAX_BODY, content_reason, system_path


@pytest.mark.parametrize(
    "name,raw",
    [
        (".env", b"password=syntheticPassword1234"),
        (".ssh/id_rsa", b"-----BEGIN PRIVATE KEY-----"),
        ("credentials.json", b"api_key=syntheticCredential1234"),
        ("tls.key", b"https://synthetic:password@example.invalid/"),
        ("ordinary.md", b"person@example.invalid /Users/synthetic/repo"),
    ],
)
def test_sensitivity_is_not_inferred_from_name_or_content(tmp_path, name, raw):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    assert not system_path(name)
    assert content_reason(raw) is None
    assert read_working(tmp_path, name.encode())[:2] == (raw, None)
    assert content_reason(raw + b"\0") == "binary"
    assert content_reason(b"\xff") == "non_utf8"
    assert content_reason(b"x" * (MAX_BODY + 1)) == "large"


def test_secret_folder_is_ignored_and_absent_from_all_collection_surfaces(
    db, git, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    # Force-tracked, nested, mixed-case, ignored and untracked folder variants.
    for prefix in (
        ".project-log-secrets",
        "nested/.project-log-secrets",
        "mixed/.PROJECT-LOG-SECRETS",
    ):
        commit(git, root, f"{prefix}/key", "synthetic-private-storage\n", "Synthetic fixture")
    commit(git, root, "public.md", "public before\n", "Synthetic public")
    git("mv", "public.md", ".project-log-secrets/moved-in.md")
    git("commit", "-m", "Synthetic move in")
    git("mv", ".project-log-secrets/key", "moved-out.md")
    git("commit", "-m", "Synthetic move out")
    (root / "docs/.project-log-secrets").mkdir(parents=True)
    (root / "docs/.project-log-secrets/key").write_text("synthetic-ignored-doc\n")
    (root / "docs/.git").mkdir()
    (root / "docs/.git/config").write_text("internal Git metadata\n")
    ignore = Path(__file__).resolve().parents[1] / ".gitignore"
    (root / ".gitignore").write_bytes(ignore.read_bytes())
    (root / ".project-log-secrets/untracked").write_text("synthetic-untracked\n")
    assert git("check-ignore", "--no-index", ".project-log-secrets/untracked")
    original_read, original_blob = collector.read_working, Git.blob

    def guarded_read(root, path):
        assert not system_path(path)
        return original_read(root, path)

    def guarded_blob(self, oid, path, mode="100644"):
        assert not system_path(path)
        return original_blob(self, oid, path, mode)

    monkeypatch.setattr(collector, "read_working", guarded_read)
    monkeypatch.setattr(Git, "blob", guarded_blob)
    before = (git("rev-parse", "HEAD"), git("show-ref"), (root / ".git/index").read_bytes())
    projects, cid = register(db, root)
    row = finish(db, cid)
    for surface in (
        "collections",
        "collection_issues",
        "working_entries",
        "working_repairs",
        "collection_repairs",
        "commits",
        "changes",
        "head_files",
        "git_commits",
        "git_changes",
        "git_files",
        "working_records",
        "commit_records",
        "change_records",
        "head_records",
    ):
        where = (
            "id=%s"
            if surface == "collections"
            else ("collection_id=%s" if surface == "collection_issues" else "project_id=%s")
        )
        for record in db.all(
            f"SELECT to_jsonb(t) AS data FROM {surface} t WHERE {where}",
            (cid if surface in {"collections", "collection_issues"} else row["project_id"],),
        ):
            assert not any(system_path(p) for p in self_workflow.metadata_paths(record["data"]))
    changes = db.all("SELECT metadata,diff FROM change_records WHERE collection_id=%s", (cid,))
    assert any(
        r["metadata"]["status"] == "D" and r["metadata"]["old"]["path"] == "public.md"
        for r in changes
    )
    assert any(
        r["metadata"]["status"] == "A" and r["metadata"]["new"]["path"] == "moved-out.md"
        for r in changes
    )
    snapshot = ExperienceInputs(db, Materials(db)).prepare(str(row["project_id"]))
    assert not any(
        system_path(p)
        for e in snapshot.entries
        for p in self_workflow.metadata_paths(json.loads(e.source_json))
    )
    with pytest.raises(CollectionError) as exc:
        projects.source(
            cid,
            git("rev-parse", "HEAD").decode(),
            base64.b64encode(b".project-log-secrets/moved-in.md").decode(),
        )
    assert exc.value.code == "not_found"
    assert (root / ".project-log-secrets/moved-in.md").is_file()
    assert read_working(root, b".project-log-secrets/moved-in.md") == (None, "system_path", {})
    assert before == (git("rev-parse", "HEAD"), git("show-ref"), (root / ".git/index").read_bytes())


@pytest.mark.parametrize("reason", ["sensitive_path", "suspected_secret"])
def test_legacy_exclusions_are_not_recovered_from_git_or_new_collection(
    db, git, tmp_path, monkeypatch, reason
):
    root = tmp_path / "repo"
    oid = commit(git, root, ".env", "synthetic old excluded\n", "Synthetic old policy")
    projects, cid = register(db, root)
    row = finish(db, cid)
    # Model historical v5 records explicitly; do not keep the removed detector for fixtures.
    db.execute("UPDATE collections SET policy=%s WHERE id=%s", (Jsonb({"version": 5}), cid))
    db.execute(
        "UPDATE changes SET content_id=NULL,body_reason=%s WHERE collection_id=%s",
        (reason, cid),
    )
    db.execute(
        "UPDATE working_entries SET content_id=NULL,sha256=NULL,body_reason=%s "
        "WHERE collection_id=%s",
        (reason, cid),
    )
    original = db.all("SELECT * FROM change_records WHERE collection_id=%s", (cid,))
    path = original[0]["metadata"]["new"]["path_b64"]
    reads = []
    object_read = Git.object_read

    def guarded(self, *args, **kwargs):
        if "blob" in args:
            reads.append(args)
        return object_read(self, *args, **kwargs)

    monkeypatch.setattr(Git, "object_read", guarded)
    assert projects.source(cid, oid, path)["body_reason"] == reason
    snapshot = ExperienceInputs(db, Materials(db)).prepare(
        str(row["project_id"]), end_collection=cid
    )
    assert any(e.reason == reason for e in snapshot.entries)
    assert not any(e.text == "synthetic old excluded\n" for e in snapshot.entries)
    assert not reads
    fresh = projects.collect_now(str(row["project_id"]))
    finish(db, str(fresh["id"]))
    assert projects.source(str(fresh["id"]), oid, path)["body"] == "synthetic old excluded\n"
    assert db.all("SELECT * FROM change_records WHERE collection_id=%s", (cid,)) == original
    frozen = replace(snapshot, policy="experience-input-v3-user-reviewed")
    assert decode_snapshot(encode_snapshot(frozen), frozen.manifest(), frozen.digest) == frozen


def test_secret_folder_permissions_and_metadata_only_configuration(tmp_path, monkeypatch):
    folder = tmp_path / ".project-log-secrets"
    folder.mkdir(mode=0o700)
    key = folder / "key"
    key.write_text("synthetic-key")
    key.chmod(0o600)
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(key))
    assert configured_profile() is not None
    assert read_operator_key(key) == "synthetic-key"
    for permissions in (0o755, 0o770):
        folder.chmod(permissions)
        assert configured_profile() is None
        with pytest.raises(CollectionError):
            read_operator_key(key)
    folder.chmod(0o700)
    alias = tmp_path / "alias/.project-log-secrets"
    alias.parent.mkdir()
    alias.symlink_to(folder, target_is_directory=True)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(alias / "key"))
    assert configured_profile() is None
    with pytest.raises(CollectionError):
        read_operator_key(alias / "key")
    ordinary = tmp_path / "key"
    ordinary.write_text("synthetic-key")
    ordinary.chmod(0o600)
    with pytest.raises(CollectionError):
        read_operator_key(ordinary)


def test_private_folder_cannot_itself_be_registered_as_a_repository(db, git, tmp_path):
    from project_log.git import diagnose

    root = tmp_path / "repo/.project-log-secrets"
    root.mkdir(mode=0o700)
    git("init", "-b", "main", str(root))
    for candidate in (root, root / ".git"):
        with pytest.raises(CollectionError) as exc:
            diagnose(str(candidate))
        assert exc.value.code == "system_path"
    alias = tmp_path / "public-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(CollectionError) as exc:
        register(db, alias)
    assert exc.value.code == "system_path"
    assert not db.all("SELECT id FROM projects")


def test_new_notice_invalidates_old_consent_without_checkpoint_compatibility_shortcut(db, tmp_path):
    pid, cid, _, _, svc = fixture(db, tmp_path)
    current = public_profile(svc.profile)
    svc.profile = {
        **current,
        "notice_version": "analysis-consent-user-reviewed-collection-safety-2026-10-19",
    }
    old = svc.profile
    prep = prepared(svc, pid, cid)
    svc.profile = current
    assert svc.preparation(pid, str(prep["id"]))["state"] == "stale"
    rejected = svc.submit(pid, submission(prep))
    assert rejected["state"] == "rejected"
    with pytest.raises(CollectionError) as exc:
        compatibility_policy(old, current)
    assert exc.value.code == "analysis_policy_changed"
    row = db.one(
        "SELECT accepted_at,input_payload FROM analysis_requests WHERE id=%s", (rejected["id"],)
    )
    assert row["accepted_at"] is None and row["input_payload"] is None
    profile = public_profile({"mode": "live"})
    assert profile["notice_version"].endswith("2026-10-28")
    assert "자동 판별" in profile["transfer_scope"]
