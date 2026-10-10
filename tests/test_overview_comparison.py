"""Dashboard counts compare surviving completed records, not capture-time baselines."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from project_log.api import create_app
from project_log.browser import Browser
from project_log.collections import Collections
from project_log.collector import collect
from project_log.service import Projects, Registration


def version(root, git, files, documents, commits=1):
    root.joinpath("docs").mkdir(exist_ok=True)
    for directory, count, extension in ((root, files, "txt"), (root / "docs", documents, "md")):
        for file in directory.glob(f"*.{extension}"):
            file.unlink()
        for i in range(count):
            (directory / f"{i}.{extension}").write_text(f"file {i}\n")
    for i in range(commits):
        (root / "0.txt").write_text(f"version {files}/{documents}/{i}\n")
        git("add", ".")
        git("commit", "-m", f"version {files}/{documents}/{i}")


def capture(db, root, pid=None):
    projects = Projects(db)
    if pid is None:
        p = projects.register(
            Registration(path=str(root), name="comparison", status="new", base_branch="main")
        )
        pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    else:
        cid = str(projects.collect_now(pid)["id"])
    collect(db, cid, lambda: False)
    return pid, cid


def evidence(db, cid):
    return {
        table: db.all(f"SELECT * FROM {table} WHERE collection_id=%s ORDER BY id", (cid,))
        for table in ("working_records", "head_records", "change_records")
    } | {
        "commits": db.all(
            "SELECT * FROM commit_records WHERE collection_id=%s ORDER BY oid", (cid,)
        ),
        "snapshot": db.detail(cid)["snapshot"],
    }


def test_example_deletion_uses_surviving_predecessor_via_api_and_preserves_evidence(
    db, git, tmp_path
):
    root = tmp_path / "repo"
    version(root, git, 50, 5, 3)
    pid, a = capture(db, root)
    version(root, git, 56, 5, 2)
    _, b = capture(db, root, pid)
    version(root, git, 60, 8)
    _, c = capture(db, root, pid)
    before_a, before_c = evidence(db, a), evidence(db, c)
    with TestClient(create_app(db, start_worker=False)) as client:

        def overview(cid):
            r = client.get(f"/api/collections/{cid}/overview")
            assert r.status_code == 200
            return r.json()

        for cid, counts, delta in (
            (a, [3, 50, 5], [None, None, None]),
            (b, [5, 56, 5], [2, 6, 0]),
            (c, [6, 60, 8], [1, 4, 3]),
        ):
            data = overview(cid)
            assert list(data["selected"]["counts"].values()) == counts
            assert list(data["delta"].values()) == delta
        assert client.delete(f"/api/collections/{b}").status_code == 200
        assert client.get(f"/api/collections/{b}").status_code == 404
        for _ in range(2):  # Fresh HTTP reads and selection of A before returning to C.
            assert overview(a)["previous"] is None
            data = overview(c)
            assert data["baseline_id"] == a
            assert data["delta"] == {"commits": 3, "files": 10, "documents": 3}
        assert [row["id"] for row in client.get(f"/api/projects/{pid}").json()["collections"]] == [
            c,
            a,
        ]
        # Stored capture comparison is detached by the existing deletion policy, not rewritten to A.
        assert db.detail(c)["baseline_id"] is None
        assert db.detail(c)["summary"]["comparison"]["reason"] == "baseline_deleted"
    assert evidence(db, a) == before_a
    assert evidence(db, c) == before_c


@pytest.mark.parametrize("deleted", [[0], [1], [3], [1, 2], [0, 1, 2]])
def test_first_middle_last_and_consecutive_deletions_with_negative_and_zero(
    db, git, tmp_path, deleted
):
    root = tmp_path / "repo"
    records = []
    pid = None
    for files, docs in ((3, 2), (4, 2), (2, 1)):
        version(root, git, files, docs)
        pid, cid = capture(db, root, pid)
        records.append(cid)
    _, cid = capture(db, root, pid)
    records.append(cid)
    expected = [
        {"commits": 1, "files": 3, "documents": 2},
        {"commits": 2, "files": 4, "documents": 2},
        {"commits": 3, "files": 2, "documents": 1},
        {"commits": 3, "files": 2, "documents": 1},
    ]
    with TestClient(create_app(db, start_worker=False)) as client:
        for cid, counts in zip(records, expected, strict=True):
            assert (
                client.get(f"/api/collections/{cid}/overview").json()["selected"]["counts"]
                == counts
            )
        assert client.get(f"/api/collections/{records[2]}/overview").json()["delta"] == {
            "commits": 1,
            "files": -2,
            "documents": -1,
        }
        assert client.get(f"/api/collections/{records[3]}/overview").json()["delta"] == {
            "commits": 0,
            "files": 0,
            "documents": 0,
        }
        for index in deleted:
            assert client.delete(f"/api/collections/{records[index]}").status_code == 200
        previous = None
        for index, cid in enumerate(records):
            if index in deleted:
                continue
            data = client.get(f"/api/collections/{cid}/overview").json()
            assert data["baseline_id"] == (records[previous] if previous is not None else None)
            assert data["delta"] == {
                key: expected[index][key] - expected[previous][key]
                if previous is not None
                else None
                for key in expected[index]
            }
            previous = index


def test_order_scope_unavailable_and_history_beyond_recent_pages(db, git, tmp_path):
    root = tmp_path / "repo"
    version(root, git, 1, 1)
    pid, first = capture(db, root)
    records = [first]
    for _ in range(11):
        _, cid = capture(db, root, pid)
        records.append(cid)
    # A legacy completed retry is still a completed record in the display order.
    retry = str(uuid.UUID(int=(1 << 128) - 1))
    current = db.detail(records[-1])
    db.execute(
        """INSERT INTO collections(id,project_id,kind,retry_of,state,snapshot,policy,created_at)
        VALUES (%s,%s,'retry',%s,'completed',%s,'{}',%s)""",
        (retry, pid, first, Jsonb(current["snapshot"]), current["created_at"]),
    )
    assert Browser(db).overview(retry)["baseline_id"] == records[-1]
    assert Browser(db).overview(retry)["delta"]["commits"] == -1
    # Incomplete observations do not cause a jump to an older, known count.
    db.execute(
        "UPDATE collections SET snapshot=snapshot-'document_roots' WHERE id=%s", (records[-1],)
    )
    data = Browser(db).overview(retry)
    assert data["previous"]["counts"]["documents"] is None and data["delta"]["documents"] is None
    db.execute(
        "UPDATE collections SET snapshot=jsonb_set(snapshot,'{complete}','false') WHERE id=%s",
        (retry,),
    )
    assert all(v is None for v in Browser(db).overview(retry)["delta"].values())
    # Place another Project and each non-completed state between the selected completed records.
    other = tmp_path / "other"
    other.mkdir()
    from project_log.git import Git

    Git(other).run("init", "-b", "main")
    _, foreign = capture(db, other)
    between = db.detail(records[0])["created_at"] + timedelta(microseconds=1)
    db.execute("UPDATE collections SET created_at=%s WHERE id=%s", (between, foreign))
    for state in (
        "capturing",
        "queued",
        "running",
        "cancel_pending",
        "cleanup_pending",
        "partial",
        "failed",
    ):
        extra = str(uuid.uuid4())
        db.execute(
            "INSERT INTO collections(id,project_id,state,policy,created_at) "
            "VALUES (%s,%s,%s,'{}',%s)",
            (extra, pid, state, between),
        )
        assert Browser(db).overview(records[1])["baseline_id"] == first
        db.execute("DELETE FROM collections WHERE id=%s", (extra,))
    with TestClient(create_app(db, start_worker=False)) as client:
        for cid in records[1:-1]:
            assert client.delete(f"/api/collections/{cid}").status_code == 200
        assert client.delete(f"/api/collections/{retry}").status_code == 200
        assert client.get(f"/api/collections/{records[-1]}/overview").json()["baseline_id"] == first


def test_concurrent_delete_reads_one_consistent_snapshot_then_rebases(
    db, git, tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    version(root, git, 1, 1)
    pid, a = capture(db, root)
    version(root, git, 2, 1)
    _, b = capture(db, root, pid)
    version(root, git, 3, 2)
    _, c = capture(db, root, pid)
    original = Browser.inventory
    deleted = False

    def inventory(self, collection, *, connection=None):
        nonlocal deleted
        if collection == c and not deleted:
            deleted = True
            Collections(db).remove(b)
        return original(self, collection, connection=connection)

    monkeypatch.setattr(Browser, "inventory", inventory)
    with TestClient(create_app(db, start_worker=False)) as client:
        data = client.get(f"/api/collections/{c}/overview").json()
        assert data["baseline_id"] == b
        assert data["delta"] == {"commits": 1, "files": 1, "documents": 1}
        data = client.get(f"/api/collections/{c}/overview").json()
        assert data["baseline_id"] == a
        assert data["delta"] == {"commits": 2, "files": 2, "documents": 1}
