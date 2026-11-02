"""Manual review contract over invented inputs and disposable databases only."""

import base64
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_analysis_admission import fixture, prepared, service, submission
from test_analysis_api import body, prepare, setup
from test_experiences import evidence, inputs

from project_log.analysis_input import canonical
from project_log.analysis_snapshot import decode_snapshot, encode_snapshot
from project_log.api import create_app
from project_log.collector import collect
from project_log.experience_input import EXPERIENCE_INPUT_POLICY
from project_log.experiences import (
    ANALYSIS_POLICY,
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    ExperienceGraph,
    Limits,
    batches,
    discovery_schema,
    split_evidence,
)
from project_log.gemini_request import provider_schema, request_policy
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.policy import sha256
from project_log.service import Projects, Registration


def test_discovery_preview_matches_sdk_schemas_and_binds_them_to_consent(db, tmp_path, monkeypatch):
    import httpx
    from test_experience_gemini import ModelConnection, model

    def forbidden(*args, **kwargs):
        pytest.fail("No credential or external HTTP access is permitted")

    monkeypatch.setattr("project_log.gemini.read_operator_key", forbidden)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    pid, cid, store, _, svc = fixture(db, tmp_path)
    store.add(pid, "large.txt", b"Synthetic counter evidence\n" * 2000)
    store.add(pid, "example.png", b"\x89PNG\r\n\x1a\nsynthetic")
    prep = prepared(svc, pid, cid)
    cache = svc.cache[str(prep["id"])]
    snapshot = decode_snapshot(json.loads(cache.payload_json), cache.manifest, cache.digest)
    chunks = split_evidence(snapshot, Limits(**svc.profile["limits"]))
    expected = batches(chunks, Limits(**svc.profile["limits"]))
    preview = svc.transfer(pid, str(prep["id"]), download=True)
    schemas = preview["response_schemas"]["discover"]
    assert len(schemas) == len(expected) > 1
    assert prep["descriptor"]["profile"]["graph_policy"] == ANALYSIS_POLICY
    adapter = model("gemini-3.5-flash-lite")
    for index, batch in enumerate(expected):
        schema = discovery_schema(batch)
        adapter.complete(
            "discover",
            BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["discover"],
            batch,
            canonical({}),
            schema,
        )
        wire = json.loads(ModelConnection.calls[-1].body)
        assert schemas[index] == {
            "batch_index": index,
            "schema": schema.model_json_schema(),
            "provider_schema": provider_schema(schema.model_json_schema()),
        }
        assert wire["generationConfig"]["responseJsonSchema"] == schemas[index]["provider_schema"]
        assert json.loads(wire["contents"][0]["parts"][0]["text"])["evidence"] == [
            chunk.payload() for chunk in batch
        ]
    document = {
        key: preview[key]
        for key in (
            "policy",
            "input_digest",
            "system_instruction",
            "task_instructions",
            "response_schemas",
            "generation",
            "chunks",
            "later_context",
            "consolidation",
            "request_contract",
            "relation_recovery",
            "relation_recovery_digest",
            "provider_response_schemas",
        )
    }
    assert sha256(canonical(document).encode()) == preview["transfer_digest"]
    assert preview["request_contract"] == request_policy()
    assert preview["relation_recovery"] == svc.profile["relation_recovery"]
    assert preview["relation_recovery_digest"] == sha256(
        canonical(svc.profile["relation_recovery"]).encode()
    )
    assert preview["consolidation"]["max_seeds_per_batch"] == 256
    assert preview["consolidation"]["policy"] == svc.profile["consolidation_policy"]
    assert "필수 키" in preview["consolidation"]["assignment_schema"]
    assert preview["transfer_digest"] == prep["descriptor"]["transfer_digest"]
    accepted = svc.submit(pid, submission(prep))
    assert accepted["state"] == "queued"
    saved = db.one(
        "SELECT consent,input_digest FROM analysis_requests WHERE id=%s", (accepted["id"],)
    )
    assert saved["consent"]["descriptor"]["transfer_digest"] == preview["transfer_digest"]
    assert saved["input_digest"] == snapshot.digest


@pytest.mark.parametrize(
    "old_policy",
    [{"graph_policy": "experience-graph-v4-user-reviewed"}, {"retry_delay": 0.25}],
    ids=["previous-graph", "previous-503-cooldown"],
)
def test_previous_graph_preparation_and_consent_are_rejected_without_rewriting_them(
    db, tmp_path, old_policy
):
    pid, cid, _, _, svc = fixture(db, tmp_path)
    current_profile = svc.profile
    svc.profile = {**current_profile, **old_policy}
    prep = prepared(svc, pid, cid)
    before = db.one("SELECT * FROM analysis_preparations WHERE id=%s", (prep["id"],))
    svc.profile = current_profile
    assert svc.preparation(pid, str(prep["id"]))["state"] == "stale"
    rejected = svc.submit(pid, submission(prep))
    assert rejected["state"] == "rejected" and rejected["error_code"] == "analysis_policy_changed"
    assert db.one("SELECT * FROM analysis_preparations WHERE id=%s", (prep["id"],)) == before
    assert db.all("SELECT * FROM analysis_calls") == []
    assert db.all("SELECT * FROM experience_runs") == []


def test_discovery_schema_tampering_invalidates_transfer_digest_before_admission(
    db, tmp_path, monkeypatch
):
    from project_log import analysis_admission

    pid, cid, _, _, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    original = analysis_admission.discovery_schema

    def changed(chunks):
        # Different allowed IDs must change the frozen transfer contract, not the input.
        return original([replace(chunks[0], id="synthetic-tampered-id")])

    monkeypatch.setattr(analysis_admission, "discovery_schema", changed)
    with pytest.raises(CollectionError) as error:
        svc.transfer(pid, str(prep["id"]))
    assert error.value.code == "analysis_input_corrupt"
    rejected = svc.submit(pid, submission(prep))
    assert rejected["state"] == "rejected" and rejected["error_code"] == "analysis_input_corrupt"
    assert db.all("SELECT * FROM analysis_calls") == []
    assert db.all("SELECT * FROM experience_runs") == []


def test_full_preview_pages_media_and_consent_use_the_same_unmodified_input(db, tmp_path):
    pid, cid, store, _, svc = fixture(db, tmp_path)
    text = "api_key=syntheticsecretvalue /Users/synthetic-account/repo confidential\n" * 4000
    store.add(pid, "large.txt", text.encode())
    raw = b"\x89PNG\r\n\x1a\nsynthetic api_key=syntheticsecretvalue"
    store.add(pid, "example.png", raw)
    prep = prepared(svc, pid, cid)
    descriptor = prep["descriptor"]
    assert "privacy" not in descriptor and "review_record" not in descriptor
    first = svc.transfer(pid, str(prep["id"]))
    assert first["total"] > 20 and len(first["chunks"]) == 20
    assert first["all_chunks_included"] is False and first["chunk_text_truncated"] is False
    full = svc.transfer(pid, str(prep["id"]), download=True)
    assert full["all_chunks_included"] and full["media_bytes_included"]
    assert full["transfer_digest"] == first["transfer_digest"] == descriptor["transfer_digest"]
    assert full["input_digest"] == prep["input_digest"]
    actual = [None] * len(text)
    for chunk in full["chunks"]:
        if chunk["text"] and any(s.get("filename") == "large.txt" for s in chunk["sources"]):
            for i, char in enumerate(chunk["text"], chunk["offset"]):
                if actual[i] is not None:
                    assert actual[i] == char
                actual[i] = char
    assert "".join(actual) == text
    index = next(i for i, chunk in enumerate(full["chunks"]) if chunk["media"])
    assert base64.b64decode(full["inline_media"][str(index)]["base64"]) == raw
    assert svc.transfer_media(pid, str(prep["id"]), index) == ("image/png", raw)
    assert svc.transfer(pid, str(prep["id"]), offset=20)["chunks"] == full["chunks"][20:40]
    accepted = svc.submit(pid, submission(prep))
    assert accepted["state"] == "queued"
    saved = db.one("SELECT input_payload FROM analysis_requests WHERE id=%s", (accepted["id"],))
    assert any(e["text"] == text for e in saved["input_payload"]["entries"])
    assert "privacy_review" not in saved["input_payload"]


def test_collection_and_git_source_are_unmasked_in_preview_and_fixed_consent(
    db, git, tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("No credential or external HTTP access is permitted")

    monkeypatch.setattr("project_log.gemini.read_operator_key", forbidden)
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", forbidden)
    root = tmp_path / "repo"
    collected_secret = b"api_key=syntheticCollectedCredential123\n"
    safe = b"Safe evidence /Users/synthetic-account/repo\n"
    (root / "token.txt").write_bytes(collected_secret)
    (root / ".env").write_text("synthetic environment body\n")
    (root / "safe.txt").write_bytes(safe)
    (root / ".gitignore").write_text("/docs/\n")
    git("add", ".")
    git("commit", "-m", "Synthetic collection evidence")
    (root / "docs").mkdir()
    (root / "docs/private.txt").write_bytes(collected_secret)
    (root / "docs/.env").write_text("synthetic document body\n")
    p = Projects(db).register(
        Registration(
            path=str(root), name="Synthetic collection", status="ongoing", base_branch="main"
        )
    )
    pid, cid = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, cid, lambda: False)
    capture = db.detail(cid)
    assert capture["state"] == "completed" and capture["policy"]["version"] == 6
    records = db.all(
        "SELECT id,body_reason,body,sha256,content_id FROM working_records WHERE collection_id=%s",
        (cid,),
    )
    excluded = {str(r["id"]): r["body_reason"] for r in records if r["body_reason"]}
    assert not {"suspected_secret", "sensitive_path"} & set(excluded.values())
    assert all(
        r["body"] is None and r["sha256"] is None and r["content_id"] is None
        for r in records
        if str(r["id"]) in excluded
    )
    assert collected_secret in [bytes(r["body"]) for r in db.all("SELECT body FROM contents")]
    store = Materials(db)
    uploaded = "api_key=syntheticUserSuppliedCredential456 /Users/synthetic-account/repo"
    store.add(pid, "user-input.txt", uploaded.encode())
    svc = service(db, store)
    prep = prepared(svc, pid, cid)
    candidate = svc.cache[str(prep["id"])]
    fixed = decode_snapshot(
        json.loads(candidate.payload_json), candidate.manifest, candidate.digest
    )
    stored_entries = {
        json.loads(e.source_json)["record_id"]: e
        for e in fixed.entries
        if json.loads(e.source_json)["kind"] == "working_entries"
    }
    for record, reason in excluded.items():
        assert stored_entries[record].reason == reason and stored_entries[record].text is None
    historical = [
        e for e in fixed.entries if json.loads(e.source_json)["kind"] == "historical_source"
    ]
    assert any(e.text == collected_secret.decode() and e.reason is None for e in historical)
    assert any(e.text == "synthetic environment body\n" and e.reason is None for e in historical)
    assert any(e.text == uploaded and e.reason is None for e in fixed.entries)
    preview = svc.transfer(pid, str(prep["id"]), download=True)
    texts = [c["text"] for c in preview["chunks"]]
    assert uploaded in texts and safe.decode() in texts
    assert any(collected_secret.decode() in text for text in texts)
    assert preview["input_digest"] == fixed.digest == prep["input_digest"]
    assert preview["transfer_digest"] == prep["descriptor"]["transfer_digest"]
    accepted = svc.submit(pid, submission(prep))
    assert accepted["state"] == "queued"
    saved = db.one(
        "SELECT input_payload,consent FROM analysis_requests WHERE id=%s", (accepted["id"],)
    )
    assert saved["consent"]["input_digest"] == fixed.digest
    assert any(e["text"] == collected_secret.decode() for e in saved["input_payload"]["entries"])
    assert any(e["text"] == uploaded for e in saved["input_payload"]["entries"])
    assert db.detail(cid) == capture
    assert (
        db.all(
            "SELECT id,body_reason,body,sha256,content_id FROM working_records "
            "WHERE collection_id=%s",
            (cid,),
        )
        == records
    )


def test_old_preparation_and_review_remain_unchanged_and_cannot_be_reused(db, tmp_path):
    pid, cid, _, _, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    old_id = str(uuid4())
    descriptor = {
        **prep["descriptor"],
        "transfer_policy": "historical-policy",
        "review_record": {"document": {"historical": True}},
    }
    db.execute(
        "INSERT INTO analysis_preparations(id,project_id,end_collection_id,generation,state,"
        "source_fingerprint,input_digest,consent_digest,descriptor) "
        "VALUES (%s,%s,%s,%s,'ready',%s,%s,%s,%s)",
        (
            old_id,
            pid,
            cid,
            svc.generation,
            prep["source_fingerprint"],
            prep["input_digest"],
            prep["consent_digest"],
            Jsonb(descriptor),
        ),
    )
    before = db.one("SELECT * FROM analysis_preparations WHERE id=%s", (old_id,))
    assert svc.preparation(pid, old_id)["state"] == "stale"
    assert svc.submit(pid, submission({**prep, "id": old_id}))["state"] == "rejected"
    assert db.one("SELECT * FROM analysis_preparations WHERE id=%s", (old_id,)) == before


def test_legacy_snapshot_decode_keeps_its_digest_but_graph_requires_new_policy():
    legacy = replace(
        inputs(evidence("legacy", "synthetic original")),
        policy="experience-input-v2",
        privacy_review_json=canonical({"historical": True}),
    )
    payload = encode_snapshot(legacy)
    loaded = decode_snapshot(payload, legacy.manifest(), legacy.digest)
    assert loaded == legacy and loaded.digest == legacy.digest

    class Never:
        def complete(self, *args):
            pytest.fail("Legacy input must never enter a model")

    with pytest.raises(CollectionError) as error:
        ExperienceGraph(Never()).run(loaded)
    assert error.value.code == "analysis_policy_changed"
    assert inputs().policy == EXPERIENCE_INPUT_POLICY


def test_preview_is_project_scoped_expires_and_rejects_changed_consent(db, tmp_path):
    pid, cid, _, _, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    with pytest.raises(CollectionError):
        svc.transfer(str(uuid4()), str(prep["id"]))
    descriptor = {**prep["descriptor"], "transfer_digest": "0" * 64}
    db.execute(
        "UPDATE analysis_preparations SET descriptor=%s WHERE id=%s",
        (Jsonb(descriptor), prep["id"]),
    )
    rejected = svc.submit(pid, submission(prep))
    assert rejected["error_code"] == "analysis_consent_mismatch"
    candidate = svc.cache[str(prep["id"])]
    svc.cache[str(prep["id"])] = replace(candidate, expires=0)
    with pytest.raises(CollectionError):
        svc.transfer(pid, str(prep["id"]))


def test_http_preview_no_store_bytes_old_review_removed_and_preparation_only(
    db, tmp_path, monkeypatch
):
    # Check API route removal independently of an incidental local Frontend build.
    monkeypatch.setenv("PROJECT_LOG_FRONTEND_DIST", str(tmp_path / "absent-frontend"))
    pid, cid, _, _, options = setup(db, tmp_path)
    options["analysis_profile"]["preparation_only"] = True
    with TestClient(create_app(db, **options)) as client:
        prep = prepare(client, pid, cid)
        path = f"/api/projects/{pid}/analysis/preparations/{prep['id']}"
        preview = client.get(path + "/transfer")
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        assert preview.json()["chunks"][0]["text"] == "Synthetic correction evidence"
        download = client.get(path + "/transfer?download=true")
        assert download.json()["all_chunks_included"]
        assert download.headers["content-disposition"].startswith("attachment;")
        assert client.get(path + "/review").status_code == 404
        assert client.post(path + "/review", json={}).status_code == 404
        assert not any("/review" in route for route in client.get("/openapi.json").json()["paths"])
        denied = client.post(f"/api/projects/{pid}/analysis/requests", json=body(prep))
        assert denied.json()["code"] == "analysis_transfer_disabled"
        assert db.all("SELECT * FROM analysis_requests") == []
        assert db.all("SELECT * FROM analysis_calls") == []
        assert db.all("SELECT * FROM experience_runs") == []
