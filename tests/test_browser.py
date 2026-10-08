import base64
from pathlib import Path

from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.browser import Browser
from project_log.collector import collect
from project_log.git import Git
from project_log.service import Projects, Registration


def collected(db, root: Path):
    p = Projects(db).register(
        Registration(path=str(root), name="Browser", status="new", base_branch="main")
    )
    cid = str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    return str(p["id"]), cid


def test_historical_structure_body_variants_and_commits(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir()
    (root / "src/main.rs").write_text("fn main() {}\n")
    (root / "docs/decisions").mkdir(parents=True)
    (root / "docs/decisions/tracked.md").write_text("Tracked then\n")
    git("add", ".")
    git("commit", "-m", "first implementation")
    oid = git("rev-parse", "HEAD").decode()
    (root / "src/main.rs").write_text('fn main() { println!("then"); }\n')
    (root / ".git/info/exclude").write_text("/docs/local/\n")
    (root / "docs/local").mkdir()
    (root / "docs/local/note.custom").write_text("Local then\n")
    (root / "docs/local/.env").write_text("private\n")
    (root / "build.sql").write_text("select 1;\n")
    pid, cid = collected(db, root)
    (root / "src/main.rs").write_text("Current filesystem must not appear\n")
    (root / "docs/local/note.custom").write_text("Current document must not appear\n")
    (root / "docs/decisions/tracked.md").write_text("Current tracked document\n")
    with TestClient(create_app(db, start_worker=False)) as client:
        overview = client.get(f"/api/collections/{cid}/overview").json()
        assert overview["selected"]["counts"] == {"commits": 1, "files": 2, "documents": 3}
        assert overview["previous"] is None
        assert all(v is None for v in overview["delta"].values())
        nodes = client.get(f"/api/collections/{cid}/browse/files").json()["items"]
        assert {n["name"] for n in nodes} == {"src", "build.sql"}
        folder = next(n for n in nodes if n["name"] == "src")
        file = client.get(
            f"/api/collections/{cid}/browse/files", params={"directory_b64": folder["path_b64"]}
        ).json()["items"][0]
        assert file["path"] == "src/main.rs"
        assert {v["layer"] for v in file["observations"]} == {"head", "index", "working"}
        for variant in file["observations"]:
            if variant["layer"] == "head":
                body = client.get(
                    f"/api/collections/{cid}/source",
                    params={"commit": oid, "path_b64": file["path_b64"]},
                ).json()
                assert body["body"] == "fn main() {}\n"
                assert body["provenance"] == "original_git_object_read_now"
            else:
                body = client.get(
                    f"/api/collections/{cid}/content/working_entries/{variant['id']}"
                ).json()
                assert "Current filesystem" not in body["body"]
        docs = client.get(f"/api/collections/{cid}/browse/documents").json()
        assert {n["path"] for n in docs["items"]} == {"docs/local/", "docs/decisions/"}
        local = client.get(
            f"/api/collections/{cid}/browse/documents",
            params={"directory_b64": base64.b64encode(b"docs/local/").decode()},
        ).json()["items"]
        for file in local:
            variant = file["observations"][0]
            body = client.get(
                f"/api/collections/{cid}/content/working_entries/{variant['id']}"
            ).json()
            if file["name"] == ".env":
                assert body == {"body": None, "body_reason": "sensitive_path"}
            else:
                assert body["body"] == "Local then\n"
        commits = client.get(f"/api/collections/{cid}/commits").json()
        assert commits["total"] == 1
        assert commits["items"][0]["oid"] == oid
        assert commits["items"][0]["metadata"]["parents"] == []
        changes = client.get(f"/api/collections/{cid}/commits/{oid}/changes").json()
        assert changes["total"] == 2
        row = next(c for c in changes["items"] if c["metadata"]["new"]["path"] == "src/main.rs")
        diff = client.get(f"/api/collections/{cid}/content/changes/{row['id']}").json()
        assert "+fn main() {}" in diff["body"]
        assert client.get(f"/api/collections/{cid}/commits/{'0' * 40}/changes").status_code == 404
    assert Projects(db).project(pid)["status"] == "new"


def test_overview_baseline_incomplete_and_legacy(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir()
    (root / "docs/a.md").write_text("one\n")
    (root / "source.hs").write_text("main = print 1\n")
    pid, first = collected(db, root)
    projects = Projects(db)
    (root / "docs/b.md").write_text("two\n")
    (root / "config.toml").write_text("value = 1\n")
    manual = str(projects.collect_now(pid)["id"])
    collect(db, manual, lambda: False)
    browser = Browser(db)
    result = browser.overview(manual)
    assert result["baseline_id"] == first
    assert result["delta"] == {"commits": 0, "files": 1, "documents": 1}
    later = str(projects.collect_now(pid)["id"])
    collect(db, later, lambda: False)
    assert browser.overview(later)["baseline_id"] == manual  # not retry's newer row
    assert browser.overview(later)["delta"] == {"commits": 0, "files": 0, "documents": 0}
    db.execute("UPDATE collections SET snapshot=snapshot - 'document_roots' WHERE id=%s", (first,))
    assert browser.overview(first)["selected"]["counts"]["documents"] is None
    db.execute(
        "UPDATE collections SET snapshot=jsonb_set(snapshot,'{complete}','false') WHERE id=%s",
        (later,),
    )
    assert all(v is None for v in browser.overview(later)["selected"]["counts"].values())


def test_directory_pagination_and_project_isolation(db, git, tmp_path):
    root = tmp_path / "repo"
    for i in range(105):
        (root / f"{i:03}.sql").write_text("select 1;\n")
    _, cid = collected(db, root)
    browser = Browser(db)
    first = browser.directory(cid, "files", "", 100, 0)
    next_page = browser.directory(cid, "files", "", 100, 100)
    assert first["total"] == next_page["total"] == 105
    assert len(first["items"]) == 100 and len(next_page["items"]) == 5
    assert {n["path_b64"] for n in first["items"]}.isdisjoint(
        n["path_b64"] for n in next_page["items"]
    )
    other = tmp_path / "other"
    other.mkdir()
    Git(other).run("init", "-b", "main")
    _, other_id = collected(db, other)
    record = first["items"][0]["observations"][0]
    with TestClient(create_app(db, start_worker=False)) as client:
        assert (
            client.get(
                f"/api/collections/{other_id}/content/working_entries/{record['id']}"
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/collections/{cid}/browse/documents", params={"directory_b64": "%%%"}
            ).status_code
            == 422
        )


def test_submodule_pointer_remains_visible_without_counting_it_as_a_file(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "main.py").write_text("print(1)\n")
    git("add", ".")
    git("commit", "-m", "implementation")
    oid = git("rev-parse", "HEAD").decode()
    git("update-index", "--add", "--cacheinfo", "160000", oid, "deps")
    git("commit", "-m", "module pointer")
    _, cid = collected(db, root)
    browser = Browser(db)
    assert browser.overview(cid)["selected"]["counts"] == {"commits": 2, "files": 1, "documents": 0}
    nodes = browser.directory(cid, "files", "", 100, 0)["items"]
    module = next(n for n in nodes if n["name"] == "deps")
    assert len(module["observations"]) == 3
    assert {o["body_reason"] for o in module["observations"] if o["layer"] != "head"} == {
        "submodule"
    }
