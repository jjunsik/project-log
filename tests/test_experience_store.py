from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_experiences import OfflineModel, evidence, inputs
from test_materials import project

from project_log.api import create_app
from project_log.collector import collect
from project_log.experience_input import Evidence, Media
from project_log.experience_store import Experiences
from project_log.experiences import ExperienceGraph
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.project_deletion import ProjectDeletion
from project_log.service import Projects, Registration


def saved(db, tmp_path):
    pid = project(db, tmp_path)
    snapshot = replace(
        inputs(
            evidence("one", "counter correction"),
            Evidence("media", "{}", media=(Media("fixture.png", "image/png", b"synthetic"),)),
        ),
        project_id=pid,
    )
    result = ExperienceGraph(OfflineModel()).run(snapshot)
    return pid, snapshot, result, Experiences(db).save(snapshot, result, model="offline")


def test_saved_revision_and_evidence_survive_source_deletion_project_isolated(db, tmp_path):
    pid, snapshot, result, run = saved(db, tmp_path)
    store = Experiences(db)
    assert store.detail(pid, str(run["id"]))["result"] == result.model_dump()
    assert store.listing(pid)[0]["candidate_count"] == len(result.candidates)
    body = store.evidence(pid, str(run["id"]), "one")
    assert body["body"] == "counter correction"
    metadata = store.evidence(pid, str(run["id"]), "media")["media"][0]
    assert "base64" not in metadata
    assert store.media(pid, str(run["id"]), "media", 0)[1] == b"synthetic"
    # New revisions never overwrite prior candidates or the input manifest.
    newer = store.save(snapshot, result, model="offline")
    assert newer["id"] != run["id"] and len(store.listing(pid)) == 2
    assert store.detail(pid, str(run["id"]))["input_manifest"] == snapshot.manifest()
    other = project(db, tmp_path, "other")
    for call in (
        lambda: store.detail(other, str(run["id"])),
        lambda: store.evidence(other, str(run["id"]), "one"),
        lambda: store.media(other, str(run["id"]), "media", 0),
    ):
        with pytest.raises(CollectionError) as exc:
            call()
        assert exc.value.code == "not_found"
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO experience_evidence(run_id,project_id,reference,source) "
            "VALUES (%s,%s,'foreign','{}')",
            (str(run["id"]), other),
        )
    ProjectDeletion(db, Materials(db)).delete(pid, "one")
    assert store.listing(other) == []
    assert db.all("SELECT id FROM experience_runs WHERE project_id=%s", (pid,)) == []


def test_atomic_failure_keeps_existing_revisions_and_rejects_wrong_digest(db, tmp_path):
    pid, snapshot, result, run = saved(db, tmp_path)
    store = Experiences(db)
    before = store.listing(pid)
    with pytest.raises(CollectionError):
        store.save(replace(snapshot, prepared_at="different"), result, model="offline")
    duplicate = replace(snapshot, entries=(snapshot.entries[0], snapshot.entries[0]))
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.save(duplicate, ExperienceGraph(OfflineModel()).run(duplicate), model="offline")
    assert store.listing(pid) == before
    assert store.detail(pid, str(run["id"]))["result"] == result.model_dump()


def test_api_graph_storage_reload_and_production_gate(db, git, tmp_path, monkeypatch):
    (tmp_path / "repo/counter.py").write_text("counter = 0\n")
    git("add", ".")
    git("commit", "-m", "counter correction")
    p = Projects(db).register(
        Registration(
            path=str(tmp_path / "repo"), name="synthetic", status="ongoing", base_branch="main"
        )
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    with TestClient(
        create_app(db, start_worker=False, experience_test_model=OfflineModel())
    ) as client:
        response = client.post(f"/api/projects/{pid}/experiences", json={})
        assert response.status_code == 201
        run = response.json()
        assert run["mode"] == "isolated" and run["result"]["candidates"]
    with TestClient(create_app(db, start_worker=False)) as client:
        assert (
            client.get(f"/api/projects/{pid}/experiences/{run['id']}").json()["result"]
            == run["result"]
        )
        listing = client.get(f"/api/projects/{pid}/experiences").json()
        assert len(listing["runs"]) == 1 and listing["transfer_enabled"] is False
        ref = run["result"]["candidates"][0]["evidence_ids"][0]
        assert (
            client.get(
                f"/api/projects/{pid}/experiences/{run['id']}/evidence", params={"reference": ref}
            ).status_code
            == 200
        )
        other = project(db, tmp_path, "other")
        assert client.get(f"/api/projects/{other}/experiences/{run['id']}").status_code == 404

        def forbidden(*args, **kwargs):
            pytest.fail("Input/credential/network access before production gate")

        monkeypatch.setattr("project_log.api.ExperienceInputs", forbidden)
        failed = client.post(f"/api/projects/{pid}/experiences", json={"confirmed": True})
        assert failed.status_code == 422 and failed.json()["code"] == "analysis_transfer_disabled"
        assert len(client.get(f"/api/projects/{pid}/experiences").json()["runs"]) == 1
