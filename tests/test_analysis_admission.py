"""Synthetic DB/storage only; no operator key or external AI is used."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from pydantic import ValidationError
from test_materials import project, stored_files

from project_log import analysis_admission as admission_module
from project_log.analysis_admission import AnalysisAdmission, PrepareAnalysis, SubmitAnalysis
from project_log.analysis_execution import execution_profile
from project_log.analysis_snapshot import decode_snapshot, encode_snapshot
from project_log.collections import Collections, Memo
from project_log.collector import collect
from project_log.db import Database
from project_log.experience_input import Evidence, ExperienceInput, Media
from project_log.experience_store import Experiences
from project_log.experiences import AnalysisResult
from project_log.git import CollectionError
from project_log.materials import Materials
from project_log.project_deletion import ProjectDeletion
from project_log.service import Projects, Registration, Settings
from scripts.verification_db import temporary_database


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No credential/provider access is permitted by admission")

    monkeypatch.setattr("project_log.gemini.read_operator_key", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def collection(db, pid, *, state="completed"):
    cid = str(uuid4())
    db.execute(
        "INSERT INTO collections(id,project_id,state,snapshot,policy) "
        "VALUES (%s,%s,%s,'{\"complete\":true}','{}')",
        (cid, pid, state),
    )
    return cid


def service(db, store):
    generation = str(uuid4())
    db.execute("UPDATE analysis_runtime SET generation=%s,ready=true", (generation,))
    return AnalysisAdmission(
        db,
        store,
        generation,
        execution_profile("synthetic-offline"),
    )


def prepared(svc, pid, cid):
    result = svc.prepare(pid, PrepareAnalysis(preparation_id=uuid4(), end_collection_id=UUID(cid)))
    assert result["state"] == "ready", result
    return result


def submission(row, **kwargs):
    return SubmitAnalysis(
        request_id=uuid4(),
        preparation_id=row["id"],
        input_digest=row["input_digest"],
        consent_digest=row["consent_digest"],
        relation_recovery_digest=row["descriptor"].get("relation_recovery_digest"),
        confirmed=True,
    ).model_copy(update=kwargs)


def fixture(db, tmp_path):
    pid = project(db, tmp_path)
    db.execute("UPDATE projects SET base_branch='main' WHERE id=%s", (pid,))
    cid = collection(db, pid)
    store = Materials(db)
    material = store.add(pid, "context.md", b"Synthetic counter correction evidence")
    svc = service(db, store)
    return pid, cid, store, material, svc


def finish(db, request):
    # Simulates only terminal DB state for admission tests; no Worker claim is implied.
    db.execute(
        "UPDATE analysis_requests SET state='failed',finished_at=clock_timestamp(),"
        "error_code='synthetic_terminal' WHERE id=%s",
        (request,),
    )


def test_prefix_materials_after_endpoint_and_no_durable_preacceptance_bytes(db, git, tmp_path):
    root = tmp_path / "repo"
    (root / "counter.py").write_text("value = -1\n")
    git("add", ".")
    git("commit", "-m", "synthetic initial")
    projects = Projects(db)
    p = projects.register(
        Registration(path=str(root), name="synthetic", status="ongoing", base_branch="main")
    )
    pid, first = str(p["id"]), str(p["collections"][0]["id"])
    collect(db, first, lambda: False)
    (root / "counter.py").write_text("value = 0\n")
    git("add", ".")
    git("commit", "-m", "synthetic correction")
    second = str(projects.collect_now(pid)["id"])
    collect(db, second, lambda: False)
    store = Materials(db)
    material = store.add(pid, "context.md", b"Added after selected endpoint")
    svc = service(db, store)
    prep = prepared(svc, pid, first)
    assert prep["descriptor"]["collection_ids"] == [first]
    assert [m["id"] for m in prep["descriptor"]["materials"]] == [str(material["id"])]
    frozen = json.loads(svc.cache[str(prep["id"])].payload_json)
    assert any(e["text"] == "value = -1\n" for e in frozen["entries"])
    assert not any(e["text"] == "value = 0\n" for e in frozen["entries"])
    assert db.all("SELECT * FROM analysis_requests") == []
    columns = db.all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='analysis_preparations'"
    )
    assert not {"body", "input_payload", "media"} & {r["column_name"] for r in columns}
    # Display memo and out-of-scope Collection changes do not invalidate consent.
    Collections(db).memo(first, Memo(title="new display label"))
    third = str(projects.collect_now(pid)["id"])
    collect(db, third, lambda: False)
    assert svc.preparation(pid, str(prep["id"]))["state"] == "ready"
    accepted = svc.submit(pid, submission(prep))
    assert accepted["state"] == "queued"
    saved = db.one("SELECT input_manifest FROM analysis_requests WHERE id=%s", (accepted["id"],))
    assert [c["id"] for c in saved["input_manifest"]["collections"]] == [first]
    # New Collection capture/completion uses its own lifecycle even during AI analysis.
    fourth = str(projects.collect_now(pid)["id"])
    collect(db, fourth, lambda: False)
    assert db.detail(fourth)["state"] == "completed"
    failed = str(projects.collect_now(pid)["id"])
    db.mark_cleanup(failed)
    Collections(db).remove(failed, cleanup=True)
    assert svc.request(pid, str(accepted["id"]))["state"] == "queued"
    assert (
        db.one("SELECT input_manifest FROM analysis_requests WHERE id=%s", (accepted["id"],))
        == saved
    )


def test_accept_exact_snapshot_consent_replay_conflict_and_project_isolation(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    body = submission(prep)
    accepted = svc.submit(pid, body)
    assert accepted["accepted_at"] is not None and accepted["state"] == "queued"
    row = db.one("SELECT * FROM analysis_requests WHERE id=%s", (body.request_id,))
    snapshot = decode_snapshot(row["input_payload"], row["input_manifest"], row["input_digest"])
    assert snapshot.entries[0].text == "Synthetic counter correction evidence"
    assert row["consent"]["confirmed"] is True
    assert row["consent"]["consent_digest"] == prep["consent_digest"]
    assert str(prep["id"]) not in svc.cache
    assert svc.submit(pid, body) == accepted  # lost response, even after cache disposal
    with pytest.raises(CollectionError, match="같은 요청 ID"):
        svc.submit(pid, body.model_copy(update={"confirmed": False}))
    other = project(db, tmp_path, "other")
    with pytest.raises(CollectionError) as exc:
        svc.request(other, str(body.request_id))
    assert exc.value.code == "not_found"
    assert svc.listing(other) == []
    finish(db, str(body.request_id))
    # Source removal does not cascade into accepted inputs or rewrite their scope.
    store.delete(pid, str(material["id"]))
    Collections(db).remove(cid)
    assert svc.submit(pid, body)["state"] == "failed"
    assert (
        svc.prepare(pid, PrepareAnalysis(preparation_id=prep["id"], end_collection_id=UUID(cid)))[
            "state"
        ]
        == "consumed"
    )
    assert (
        db.one("SELECT input_payload FROM analysis_requests WHERE id=%s", (body.request_id,))[
            "input_payload"
        ]
        == row["input_payload"]
    )
    ProjectDeletion(db, store).delete(pid, "one")
    assert db.all("SELECT * FROM analysis_requests WHERE project_id=%s", (pid,)) == []
    assert db.all("SELECT * FROM analysis_preparations WHERE project_id=%s", (pid,)) == []


@pytest.mark.parametrize("change", ["consent", "input", "confirmation"])
def test_mismatched_consent_is_durably_rejected_without_input_copy(db, tmp_path, change):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    changes = {
        "consent": {"consent_digest": "0" * 64},
        "input": {"input_digest": "0" * 64},
        "confirmation": {"confirmed": False},
    }
    body = submission(prep, **changes[change])
    rejected = svc.submit(pid, body)
    assert rejected["state"] == "rejected" and rejected["accepted_at"] is None
    assert rejected["error_code"] == "analysis_consent_mismatch"
    assert svc.submit(pid, body) == rejected
    assert db.one("SELECT input_payload FROM analysis_requests")["input_payload"] is None
    store.add(pid, "unlocked.md", b"Not accepted, so mutable")


def test_boolean_coercion_not_consent():
    body = dict(
        request_id=str(uuid4()),
        preparation_id=str(uuid4()),
        input_digest="a" * 64,
        consent_digest="b" * 64,
        confirmed="true",
    )
    with pytest.raises(ValidationError):
        SubmitAnalysis.model_validate(body)
    body["confirmed"] = True
    assert SubmitAnalysis.model_validate(body).confirmed is True


@pytest.mark.parametrize(
    "change", ["upload", "delete_material", "settings", "delete_end", "collection_policy"]
)
def test_source_changes_stale_and_reject_previous_consent(db, tmp_path, change):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    if change == "upload":
        store.add(pid, "later.md", b"New late evidence")
    elif change == "delete_material":
        store.delete(pid, str(material["id"]))
    elif change == "settings":
        Projects(db).update(pid, Settings(name="changed", status="ongoing", base_branch="main"))
    elif change == "delete_end":
        Collections(db).remove(cid)
    else:
        db.execute('UPDATE collections SET policy=\'{"version":"changed"}\' WHERE id=%s', (cid,))
    assert svc.preparation(pid, str(prep["id"]))["state"] == "stale"
    rejected = svc.submit(pid, submission(prep))
    assert rejected["state"] == "rejected" and rejected["accepted_at"] is None
    assert rejected["error_code"] in {"analysis_input_changed", "analysis_end_unavailable"}
    assert db.one("SELECT input_payload FROM analysis_requests")["input_payload"] is None


def test_empty_missing_endpoint_and_lost_temporary_input_are_not_accepted(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    store.delete(pid, str(material["id"]))
    empty = svc.prepare(pid, PrepareAnalysis(preparation_id=uuid4(), end_collection_id=UUID(cid)))
    assert empty["state"] == "empty" and empty["descriptor"]["processable_count"] == 0
    assert svc.submit(pid, submission(empty))["error_code"] == "analysis_no_input"
    missing = svc.prepare(pid, PrepareAnalysis(preparation_id=uuid4(), end_collection_id=uuid4()))
    assert missing["state"] == "failed" and missing["error_code"] == "analysis_end_unavailable"
    store.add(pid, "new.md", b"New local evidence")
    prep = prepared(svc, pid, cid)
    svc.cache.clear()
    assert svc.preparation(pid, str(prep["id"]))["state"] == "stale"
    assert svc.submit(pid, submission(prep))["error_code"] == "analysis_preparation_lost"


def test_concurrent_projects_admit_only_one_without_queue_or_other_project_lock(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    other = project(db, tmp_path, "other")
    other_cid = collection(db, other)
    store.add(other, "other.md", b"Other project evidence")
    preps = [prepared(svc, pid, cid), prepared(svc, other, other_cid)]
    barrier = threading.Barrier(2)

    def send(index):
        barrier.wait(timeout=5)
        return svc.submit([pid, other][index], submission(preps[index]))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(send, [0, 1]))
    assert sorted(r["state"] for r in results) == ["queued", "rejected"]
    rejected = next(r for r in results if r["state"] == "rejected")
    assert rejected["error_code"] == "analysis_capacity_full" and rejected["accepted_at"] is None
    assert (
        db.one("SELECT count(*) AS n FROM analysis_requests WHERE input_payload IS NOT NULL")["n"]
        == 1
    )
    store.add(str(rejected["project_id"]), "still-mutable.md", b"Unaccepted project stays mutable")


def test_same_request_concurrent_replay_creates_one_acceptance(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    body = submission(prep)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: svc.submit(pid, body), range(2)))
    assert results[0] == results[1] and results[0]["state"] == "queued"
    assert len(svc.listing(pid)) == 1


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_accepted_project_mutations_block_before_side_effects_internal_cleanup_allowed(
    db, tmp_path, state
):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    pending = collection(db, pid, state="queued")
    prep = prepared(svc, pid, cid)
    accepted = svc.submit(pid, submission(prep))
    db.execute("UPDATE analysis_requests SET state=%s WHERE id=%s", (state, accepted["id"]))
    before = stored_files(store), store.listing(pid)
    for mutate in (
        lambda: store.add(pid, "blocked.md", b"Blocked upload"),
        lambda: store.delete(pid, str(material["id"])),
        lambda: Collections(db).memo(cid, Memo(title="blocked")),
        lambda: Collections(db).remove(cid),
        lambda: Collections(db).cancel(pending),
        lambda: Projects(db).update(
            pid, Settings(name="blocked", status="ongoing", base_branch="main")
        ),
        lambda: ProjectDeletion(db, store).delete(pid, "one"),
    ):
        with pytest.raises(CollectionError) as exc:
            mutate()
        assert exc.value.code == "analysis_active"
    assert (stored_files(store), store.listing(pid)) == before
    # Failure cleanup is not a new user cancel; fixed inputs remain untouched.
    db.execute("UPDATE collections SET state='cleanup_pending' WHERE id=%s", (pending,))
    Collections(db).remove(pending, cleanup=True)
    assert db.all("SELECT id FROM collections WHERE id=%s", (pending,)) == []
    assert svc.request(pid, str(accepted["id"]))["state"] == state
    assert store.read(pid, str(material["id"]))[1] == b"Synthetic counter correction evidence"
    finish(db, str(accepted["id"]))
    store.add(pid, "released.md", b"Terminal releases Project")


def test_acceptance_rollback_leaves_checking_no_partial_payload_and_same_id_can_retry(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    body = submission(prep)
    db.execute(
        "ALTER TABLE analysis_requests ADD CONSTRAINT synthetic_fail CHECK (state<>'queued')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        svc.submit(pid, body)
    row = db.one("SELECT * FROM analysis_requests WHERE id=%s", (body.request_id,))
    assert (
        row["state"] == "checking" and row["accepted_at"] is None and row["input_payload"] is None
    )
    assert svc.preparation(pid, str(prep["id"]))["state"] == "ready"
    db.execute("ALTER TABLE analysis_requests DROP CONSTRAINT synthetic_fail")
    assert svc.submit(pid, body)["state"] == "queued"


def test_generation_fence_prevents_new_preparation_and_admission(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    db.execute("UPDATE analysis_runtime SET generation=%s", (uuid4(),))
    for action in (lambda: prepared(svc, pid, cid), lambda: svc.submit(pid, submission(prep))):
        with pytest.raises(CollectionError) as exc:
            action()
        assert exc.value.code == "analysis_unavailable"
    assert svc.listing(pid) == []


def test_old_generation_preparing_and_ready_need_fresh_preparation(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    ready = prepared(svc, pid, cid)
    preparing = uuid4()
    db.execute(
        "INSERT INTO analysis_preparations(id,project_id,end_collection_id,generation,state) "
        "VALUES (%s,%s,%s,%s,'preparing')",
        (preparing, pid, cid, svc.generation),
    )
    newer = service(db, store)
    for preparation in (preparing, ready["id"]):
        row = newer.preparation(pid, str(preparation))
        assert row["state"] == "stale" and row["error_code"] == "analysis_preparation_lost"


def test_008_upgrade_preserves_completed_results_evidence_and_sources(tmp_path):
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in sorted(Path("src/project_log/migrations").glob("*.sql"))[:8]:
                raw = path.read_bytes()
                conn.execute(raw.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (path.name, hashlib.sha256(raw).hexdigest()),
                )
        pid = project(db, tmp_path)
        collection(db, pid)
        snapshot = ExperienceInput(
            pid, "synthetic-time", "[]", (Evidence("old", "{}", "Old preserved evidence"),)
        )
        result = AnalysisResult(
            input_digest=snapshot.digest,
            policy="synthetic",
            candidates=[],
            coverage={},
            model_calls=0,
        )
        Experiences(db).save(snapshot, result, model="synthetic-offline")
        tables = ("projects", "collections", "experience_runs", "experience_evidence")
        before = {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in tables}
        ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
        db.migrate()
        db.migrate()
        assert {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in tables} == before
        assert db.all("SELECT * FROM schema_migrations ORDER BY version")[:8] == ledger
        assert db.one("SELECT ready,generation FROM analysis_runtime") == {
            "ready": False,
            "generation": None,
        }
        assert db.all("SELECT * FROM analysis_requests") == []


def test_material_mutation_before_final_validation_rejects_old_consent(db, tmp_path, monkeypatch):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    entered, release = threading.Event(), threading.Event()
    cached = svc._cached

    def pause(preparation):
        result = cached(preparation)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(svc, "_cached", pause)
    with ThreadPoolExecutor(1) as pool:
        request = pool.submit(svc.submit, pid, submission(prep))
        assert entered.wait(5)
        store.add(pid, "concurrent.md", b"Changed before admission lock")
        release.set()
        rejected = request.result(timeout=5)
    assert rejected["state"] == "rejected"
    assert rejected["error_code"] == "analysis_input_changed"
    assert db.one("SELECT input_payload FROM analysis_requests")["input_payload"] is None


def test_acceptance_before_material_mutation_blocks_mutation(db, tmp_path, monkeypatch):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    prep = prepared(svc, pid, cid)
    entered, release, mutating = threading.Event(), threading.Event(), threading.Event()
    validate = svc._reject_code

    def pause(*args):
        entered.set()  # admission now owns Project row lock
        assert release.wait(5)
        return validate(*args)

    def upload():
        mutating.set()
        return store.add(pid, "concurrent.md", b"Changed after admission lock")

    monkeypatch.setattr(svc, "_reject_code", pause)
    before = stored_files(store)
    with ThreadPoolExecutor(2) as pool:
        request = pool.submit(svc.submit, pid, submission(prep))
        assert entered.wait(5)
        mutation = pool.submit(upload)
        assert mutating.wait(5)
        release.set()
        assert request.result(timeout=5)["state"] == "queued"
        with pytest.raises(CollectionError) as exc:
            mutation.result(timeout=5)
    assert exc.value.code == "analysis_active"
    assert stored_files(store) == before


def test_unfinished_predecessor_completes_before_validation_requiring_reconsent(db, tmp_path):
    pid, pending, store, material, svc = fixture(db, tmp_path)
    db.execute("UPDATE collections SET state='running' WHERE id=%s", (pending,))
    end = collection(db, pid)
    prep = prepared(svc, pid, end)
    assert prep["descriptor"]["collection_ids"] == [end]
    with db.writer(pending) as conn:
        conn.execute("UPDATE collections SET state='completed' WHERE id=%s", (pending,))
    assert svc.submit(pid, submission(prep))["error_code"] == "analysis_input_changed"


def test_admission_locks_unfinished_predecessor_against_completion(db, tmp_path, monkeypatch):
    pid, pending, store, material, svc = fixture(db, tmp_path)
    db.execute("UPDATE collections SET state='running' WHERE id=%s", (pending,))
    end = collection(db, pid)
    prep = prepared(svc, pid, end)
    entered, release, completing = threading.Event(), threading.Event(), threading.Event()
    version = admission_module.source_version

    def pause(conn, project_id, end_id, *, lock=False):
        result = version(conn, project_id, end_id, lock=lock)
        if lock:
            entered.set()  # includes unfinished predecessors FOR SHARE
            assert release.wait(5)
        return result

    def complete_pending():
        with db.writer(pending) as conn:
            completing.set()
            conn.execute("UPDATE collections SET state='completed' WHERE id=%s", (pending,))

    monkeypatch.setattr(admission_module, "source_version", pause)
    with ThreadPoolExecutor(2) as pool:
        request = pool.submit(svc.submit, pid, submission(prep))
        assert entered.wait(5)
        completion = pool.submit(complete_pending)
        assert completing.wait(5)
        release.set()
        accepted = request.result(timeout=5)
        completion.result(timeout=5)
    assert accepted["state"] == "queued" and db.detail(pending)["state"] == "completed"
    row = db.one("SELECT input_manifest FROM analysis_requests WHERE id=%s", (accepted["id"],))
    assert [c["id"] for c in row["input_manifest"]["collections"]] == [end]


def test_database_global_project_preparation_and_payload_constraints(db, tmp_path):
    pid, cid, store, material, svc = fixture(db, tmp_path)
    one, two = prepared(svc, pid, cid), prepared(svc, pid, cid)
    accepted = svc.submit(pid, submission(one))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO analysis_requests(id,project_id,preparation_id,submission_digest,"
            "generation,state,accepted_at,input_digest,input_manifest,input_payload,consent) "
            "SELECT %s,project_id,%s,submission_digest,generation,state,accepted_at,"
            "input_digest,input_manifest,input_payload,consent FROM analysis_requests WHERE id=%s",
            (uuid4(), two["id"], accepted["id"]),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE analysis_requests SET input_payload=NULL WHERE id=%s", (accepted["id"],))
    other = project(db, tmp_path, "other")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO analysis_requests"
            "(id,project_id,preparation_id,submission_digest,generation) "
            "VALUES (%s,%s,%s,'synthetic',%s)",
            (uuid4(), other, two["id"], svc.generation),
        )


def test_snapshot_roundtrip_and_corruption_rejection():
    snapshot = ExperienceInput(
        str(uuid4()),
        "synthetic-time",
        "[]",
        (Evidence("one", "{}", "synthetic", (Media("one.png", "image/png", b"synthetic"),)),),
    )
    payload = encode_snapshot(snapshot)
    assert decode_snapshot(payload, snapshot.manifest(), snapshot.digest) == snapshot
    payload["entries"][0]["media"][0]["base64"] = "Y29ycnVwdA=="
    with pytest.raises(CollectionError) as exc:
        decode_snapshot(payload, snapshot.manifest(), snapshot.digest)
    assert exc.value.code == "analysis_input_corrupt"
