import hashlib
import io
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.db import Database
from project_log.git import CollectionError
from project_log.materials import DEFAULT_MAX_BYTES, FORMATS, Materials, filename_key
from scripts.verification_db import temporary_database


def project(db, tmp_path, name="one"):
    pid = str(uuid.uuid4())
    repository = tmp_path / name
    repository.mkdir()
    db.execute(
        "INSERT INTO projects(id,name,path,repository_key,status,coding_agent,repository_info) "
        "VALUES (%s,%s,%s,%s,'ongoing','none','{}')",
        (pid, name, str(repository), str(repository / ".git")),
    )
    return pid


def stored_files(store):
    return {path.relative_to(store.root) for path in store.root.rglob("*") if path.is_file()}


def upload(client, pid, name, raw):
    return client.put(
        f"/api/projects/{pid}/materials",
        content=raw,
        headers={"content-type": "application/octet-stream", "x-file-name": quote(name, safe="")},
    )


def test_new_and_004_upgrade_are_additive_and_idempotent(tmp_path):
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for file in sorted(Path("src/project_log/migrations").glob("*.sql"))[:4]:
                raw = file.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (file.name, hashlib.sha256(raw).hexdigest()),
                )
        pid = project(db, tmp_path)
        cid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO collections(id,project_id,state,snapshot,policy) "
            "VALUES (%s,%s,'failed','{}','{}')",
            (cid, pid),
        )
        before = {table: db.all(f"SELECT * FROM {table}") for table in ("projects", "collections")}
        db.migrate()
        ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
        assert len(ledger) == 6
        db.migrate()
        assert db.all("SELECT * FROM schema_migrations ORDER BY version") == ledger
        for row in db.all("SELECT * FROM collections"):
            assert row.pop("title") is None and row.pop("description") is None
            assert row in before["collections"]
        assert db.all("SELECT * FROM projects") == before["projects"]
        assert Materials(db, tmp_path / "storage").listing(pid) == []
        assert db.all("SELECT * FROM user_materials") == []


def test_formats_original_bytes_download_and_source_independence(db, tmp_path):
    pid = project(db, tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("[Content_Types].xml", "<Types/>")
        zipped.writestr("word/document.xml", "<document/>")
    samples = {
        ".txt": b"Text\r\n",
        ".md": "# 설계\n".encode(),
        ".log": b"Build passed\n",
        ".json": b'{"result":true}',
        ".csv": b"metric,value\ncount,3\n",
        ".pdf": b"%PDF-1.7\nsynthetic fixture\n%%EOF",
        ".docx": archive.getvalue(),
        ".png": b"\x89PNG\r\n\x1a\nsynthetic",
        ".jpg": b"\xff\xd8\xfffixture",
        ".jpeg": b"\xff\xd8\xffdifferent fixture",
    }
    assert set(samples) == set(FORMATS)
    store = Materials(db, tmp_path / "storage")
    with TestClient(create_app(db, start_worker=False, storage_root=store.root)) as client:
        policy = client.get("/api/material-policy").json()
        assert policy == {"extensions": list(FORMATS), "max_bytes": DEFAULT_MAX_BYTES}
        for extension, raw in samples.items():
            source = tmp_path / ("자료" + extension)
            source.write_bytes(raw)
            response = upload(client, pid, source.name, source.read_bytes())
            assert response.status_code == 201
            row = response.json()
            assert row["sha256"] == hashlib.sha256(raw).hexdigest()
            assert row["size_bytes"] == len(raw)
            assert "path" not in row and "collection_id" not in row
            assert store.path(pid, row["id"]).read_bytes() == raw
            moved = source.with_suffix(extension + ".moved")
            source.rename(moved)
            moved.unlink()  # Only this synthetic source fixture is removed.
            result = client.get(f"/api/projects/{pid}/materials/{row['id']}/file")
            assert result.content == raw
            assert result.headers["content-disposition"].startswith("attachment;")
            assert (
                "UTF-8''" + quote("자료" + extension, safe="")
                in result.headers["content-disposition"]
            )
            assert result.headers["x-content-type-options"] == "nosniff"
        listing = client.get(f"/api/projects/{pid}/materials").json()
        assert [row["extension"] for row in listing] == list(reversed(samples))
        assert not list((store.root / ".tmp").iterdir())
        assert len(stored_files(store)) == len(samples)


def test_duplicate_matrix_normalized_names_and_project_isolation(db, tmp_path):
    pid, other = project(db, tmp_path), project(db, tmp_path, "two")
    store = Materials(db, tmp_path / "storage")
    first = store.add(pid, "Café.txt", b"one")
    for name, raw, code in [
        ("Café.txt", b"one", "content_duplicate"),
        ("other.txt", b"one", "content_duplicate"),
        ("Café.txt", b"different", "filename_conflict"),
        ("CAFÉ.TXT", b"different", "filename_conflict"),
        ("Cafe\u0301.txt", b"different", "filename_conflict"),
        ("Ｃａｆé.txt", b"different", "filename_conflict"),
    ]:
        with pytest.raises(CollectionError) as exc:
            store.add(pid, name, raw)
        assert exc.value.code == code
    second = store.add(pid, "different.txt", b"different")
    shared_bytes = store.add(other, "Café.txt", b"one")
    assert [r["id"] for r in store.listing(pid)] == [second["id"], first["id"]]
    assert [r["id"] for r in store.listing(other)] == [shared_bytes["id"]]
    assert store.path(pid, str(first["id"])) != store.path(other, str(shared_bytes["id"]))
    with TestClient(create_app(db, start_worker=False, storage_root=store.root)) as client:
        assert (
            client.get(f"/api/projects/{pid}/materials/{shared_bytes['id']}/file").status_code
            == 404
        )
        assert (
            client.delete(f"/api/projects/{pid}/materials/{shared_bytes['id']}").status_code == 404
        )
    source = tmp_path / "original.txt"
    source.write_bytes(b"one")
    store.delete(pid, str(first["id"]))
    assert source.read_bytes() == b"one"
    assert not store.path(pid, str(first["id"])).exists()
    assert store.read(other, str(shared_bytes["id"]))[1] == b"one"
    assert len(stored_files(store)) == 2
    assert len(store.listing(pid)) == 1
    # Ownership prevents a future raw Project DELETE from silently orphaning managed copies.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM projects WHERE id=%s", (other,))
    store.delete(other, str(shared_bytes["id"]))
    assert store.read(pid, str(second["id"]))[1] == b"different"


def test_api_validation_limits_and_local_boundary(db, tmp_path):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")
    with TestClient(
        create_app(db, start_worker=False, storage_root=store.root, material_max_bytes=8)
    ) as client:
        for name, raw, code in [
            ("bad.exe", b"binary", "unsupported_format"),
            ("folder/file.txt", b"text", "invalid_filename"),
            ("fake.pdf", b"text", "invalid_format"),
            ("binary.txt", b"\0", "invalid_format"),
            ("nonutf.txt", b"\xff", "invalid_format"),
            ("fake.docx", b"PKtest", "invalid_format"),
            ("large.txt", b"123456789", "file_too_large"),
        ]:
            response = upload(client, pid, name, raw)
            assert response.status_code in {422, 413}
            assert response.json()["code"] == code
        assert upload(client, pid, "exact.txt", b"12345678").status_code == 201
        url = f"/api/projects/{pid}/materials"
        assert (
            client.put(url, content=b"x", headers={"content-type": "application/json"}).status_code
            == 415
        )
        assert (
            client.put(
                url, content=b"x", headers={"origin": "https://external.invalid"}
            ).status_code
            == 403
        )
        assert (
            client.delete(
                url + "/" + str(uuid.uuid4()), headers={"sec-fetch-site": "cross-site"}
            ).status_code
            == 403
        )
    assert len(store.listing(pid)) == 1
    assert len(stored_files(store)) == 1


def test_default_limit_rejects_oversized_request_without_storage(db, tmp_path):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")
    with TestClient(create_app(db, start_worker=False, storage_root=store.root)) as client:
        result = upload(client, pid, "large.txt", b"x" * (DEFAULT_MAX_BYTES + 1))
        assert result.status_code == 413
    assert store.listing(pid) == []
    assert stored_files(store) == set()


@pytest.mark.parametrize(
    "name",
    ["../a.txt", "a\\b.txt", "a.txt.", " a.txt", "AUX.txt", "x\n.txt", "a\u200b.txt", "a：b.txt"],
)
def test_nonportable_or_folder_names_rejected(name):
    with pytest.raises(CollectionError, match="ファイル|파일"):
        filename_key(name)


def test_storage_staging_and_db_failures_leave_no_partial_material(db, tmp_path, monkeypatch):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")
    accepted = store.add(pid, "accepted.md", b"accepted")
    baseline = stored_files(store)
    with monkeypatch.context() as patch:
        patch.setattr(
            "project_log.materials.os.replace",
            lambda *args: (_ for _ in ()).throw(OSError("synthetic storage failure")),
        )
        with TestClient(create_app(db, start_worker=False, storage_root=store.root)) as client:
            result = upload(client, pid, "failed.md", b"failure")
            assert result.status_code == 503 and result.json()["code"] == "storage_failed"
    assert stored_files(store) == baseline
    original = db.connect

    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            raise psycopg.OperationalError("synthetic failure before commit")

    with monkeypatch.context() as patch:
        patch.setattr(db, "connect", fail_commit)
        with pytest.raises(psycopg.OperationalError):
            store.add(pid, "db-failed.md", b"db failure")
    assert stored_files(store) == baseline
    assert [row["id"] for row in store.listing(pid)] == [accepted["id"]]


def test_delete_restores_copy_on_db_failure_and_preserves_metadata_on_unlink_failure(
    db, tmp_path, monkeypatch
):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")
    material = store.add(pid, "keep.txt", b"preserved")
    mid = str(material["id"])
    original = db.connect

    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            raise psycopg.OperationalError("synthetic failure before commit")

    with monkeypatch.context() as patch:
        patch.setattr(db, "connect", fail_commit)
        with pytest.raises(psycopg.OperationalError):
            store.delete(pid, mid)
    assert store.read(pid, mid)[1] == b"preserved"
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "unlink",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic unlink failure")),
        )
        with pytest.raises(OSError):
            store.delete(pid, mid)
    assert store.read(pid, mid)[1] == b"preserved"
    assert not list((store.root / ".tmp").iterdir())
    store.delete(pid, mid)
    assert store.listing(pid) == [] and not stored_files(store)


def test_concurrent_uploads_serialize_duplicates(db, tmp_path):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")

    def add(name):
        try:
            return store.add(pid, name, b"same")["filename"]
        except CollectionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ["a.txt", "b.txt"]))
    assert results.count("content_duplicate") == 1
    assert len(store.listing(pid)) == len(stored_files(store)) == 1


def test_storage_root_must_stay_outside_registered_repository(db, tmp_path):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "one/storage")
    with pytest.raises(CollectionError) as exc:
        store.add(pid, "file.txt", b"safe")
    assert exc.value.code == "storage_in_repository"
    assert store.listing(pid) == [] and not store.root.exists()


def test_stage_write_failure_cleans_temp_and_metadata(db, tmp_path, monkeypatch):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")

    def fail_sync(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("project_log.materials.os.fsync", fail_sync)
    with pytest.raises(OSError):
        store.add(pid, "failure.txt", b"partially written bytes")
    assert store.listing(pid) == [] and not stored_files(store)


def test_managed_symlinks_cannot_redirect_reads_writes_or_deletion(db, tmp_path):
    pid = project(db, tmp_path)
    store = Materials(db, tmp_path / "storage")
    row = store.add(pid, "saved.txt", b"original")
    mid = str(row["id"])
    target = store.path(pid, mid)
    source = tmp_path / "one" / mid
    source.write_bytes(b"original")
    target.rename(target.with_name("synthetic-backup"))
    target.symlink_to(source)
    for action in (lambda: store.read(pid, mid), lambda: store.delete(pid, mid)):
        with pytest.raises(OSError):
            action()
    assert source.read_bytes() == b"original"
    assert len(store.listing(pid)) == 1
    target.unlink()  # Remove only this test-created symlink.
    target.with_name("synthetic-backup").rename(target)
    target.parent.rename(target.parent.with_name("synthetic-project-backup"))
    target.parent.symlink_to(tmp_path / "one", target_is_directory=True)
    for action in (
        lambda: store.add(pid, "new.txt", b"new"),
        lambda: store.read(pid, mid),
        lambda: store.delete(pid, mid),
    ):
        with pytest.raises(OSError):
            action()
    assert [p.name for p in (tmp_path / "one").iterdir()] == [mid]
    assert source.read_bytes() == b"original"
    assert len(store.listing(pid)) == 1
    assert not list((store.root / ".tmp").iterdir())


def test_configurable_root_limit_and_relocation_without_metadata_paths(db, tmp_path, monkeypatch):
    pid = project(db, tmp_path)
    monkeypatch.setenv("PROJECT_LOG_STORAGE_ROOT", str(tmp_path / "configured"))
    monkeypatch.setenv("PROJECT_LOG_MATERIAL_MAX_BYTES", "123")
    store = Materials(db)
    assert store.max_bytes == 123 and store.root == tmp_path / "configured"
    row = store.add(pid, "portable.txt", b"portable")
    relocated = tmp_path / "relocated"
    store.root.rename(relocated)
    moved = Materials(db, relocated)
    assert moved.read(pid, str(row["id"])) == (row, b"portable")
    assert "path" not in row
    with pytest.raises(CollectionError):
        moved.check_repository(str(relocated / "nested-repo"))
