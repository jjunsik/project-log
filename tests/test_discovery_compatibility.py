"""Known legacy discovery contract only. All histories and calls here are synthetic."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_analysis_admission import submission
from test_analysis_resume import original, prepare_resume
from test_analysis_worker import accepted, setup_worker, stop, terminal
from test_experiences import OfflineModel, evidence, inputs

from project_log.analysis_api import public_profile
from project_log.analysis_checkpoints import (
    DISCOVERY_COMPATIBILITY,
    LEGACY_INSTRUCTION_DIGEST,
    contract_digest,
    previous_budget_profile,
    read_rows,
    resume_rows,
)
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_snapshot import decode_snapshot
from project_log.db import Database
from project_log.experience_input import Evidence, ExperienceInput, Media
from project_log.experience_store import Experiences
from project_log.experiences import (
    BASE_INSTRUCTION,
    TASK_INSTRUCTIONS,
    Discovery,
    Limits,
    batches,
    discovery_schema,
    split_evidence,
)
from project_log.git import CollectionError
from project_log.policy import sha256
from scripts.verification_db import temporary_database


def legacy_profile(current):
    old = {**previous_budget_profile(current), "instruction_digest": LEGACY_INSTRUCTION_DIGEST}
    old.pop("consolidation_policy")
    old.pop("discovery_contract")
    # Neither contract existed in the pinned historical metadata below.
    old.pop("request_contract", None)
    old.pop("relation_recovery", None)
    return old


def discovery_rows(snapshot, profile, *, empty_before_last=False):
    rows = []
    original_batches = batches(
        split_evidence(snapshot, Limits(**profile["limits"])), Limits(**profile["limits"])
    )
    for ordinal, batch in enumerate(original_batches, 1):
        schema = discovery_schema(batch)
        response = OfflineModel().complete(
            "discover", BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["discover"], batch, "{}", schema
        )
        if empty_before_last and ordinal < len(original_batches):
            response = Discovery(seeds=[])
        elif empty_before_last:
            response.seeds.append(
                response.seeds[0].model_copy(update={"title": "Related synthetic observation"})
            )
        data = response.model_dump()
        rows.append(
            {
                "ordinal": ordinal,
                "task": "discover",
                "contract_digest": contract_digest(snapshot.digest, profile),
                "request_digest": sha256(
                    canonical(
                        {
                            "task": "discover",
                            "instruction": BASE_INSTRUCTION + " " + TASK_INSTRUCTIONS["discover"],
                            "chunks": [c.payload() for c in batch],
                            "context": {},
                            "schema": schema.model_json_schema(),
                        }
                    ).encode()
                ),
                "response": data,
                "response_digest": sha256(canonical(data).encode()),
            }
        )
    return rows


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "model",
        "retry",
        "graph",
        "notice",
        "instruction",
        "proof",
        "request",
        "response_hash",
        "semantic_reference",
        "duplicate_reference",
        "flag_type",
    ],
)
def test_known_legacy_discovery_requires_exact_invariants_and_request_proof(change):
    new = public_profile(
        {**execution_profile("gemini-3.5-flash-lite", mode="live"), "free_tier_confirmed": True}
    )
    old = legacy_profile(new)
    snapshot = inputs(evidence("one", "counter correction"))
    rows = discovery_rows(snapshot, old)
    if change == "model":
        old["model"] = "different-model"
    elif change == "retry":
        old["retry_delay"] = 0.25
    elif change == "graph":
        old["graph_policy"] = "different-graph"
    elif change == "notice":
        old["notice_version"] = "different-notice"
    elif change == "instruction":
        old["instruction_digest"] = "0" * 64
    elif change == "proof":
        new["discovery_contract"] = "0" * 64
    elif change == "flag_type":
        old["free_tier_confirmed"] = 1
        rows[0]["contract_digest"] = contract_digest(snapshot.digest, old)
    elif change == "request":
        rows[0]["request_digest"] = "0" * 64
    elif change == "response_hash":
        rows[0]["response_digest"] = "0" * 64
    elif change in {"semantic_reference", "duplicate_reference"}:
        ref = rows[0]["response"]["seeds"][0]["evidence_ids"][0]
        rows[0]["response"]["seeds"][0]["evidence_ids"] = (
            ["unprovided"] if change == "semantic_reference" else [ref, ref]
        )
        rows[0]["response_digest"] = sha256(canonical(rows[0]["response"]).encode())
    if change == "none":
        before = canonical(rows)
        reused = resume_rows(rows, snapshot, old, new)
        assert reused[0]["contract_digest"] == contract_digest(snapshot.digest, new)
        assert reused[0]["request_digest"] == rows[0]["request_digest"]
        assert canonical(rows) == before
    else:
        with pytest.raises(CollectionError):
            resume_rows(rows, snapshot, old, new)


def test_legacy_consolidation_and_later_results_are_not_transferred():
    new = execution_profile("synthetic")
    old = legacy_profile(new)
    snapshot = inputs(evidence("one", "counter correction"))
    rows = discovery_rows(snapshot, old)
    response = {"groups": [{"seed_ids": [0]}]}
    rows.append(
        {
            "ordinal": 2,
            "task": "consolidate",
            "contract_digest": contract_digest(snapshot.digest, old),
            "request_digest": "a" * 64,
            "response": response,
            "response_digest": sha256(canonical(response).encode()),
        }
    )
    assert len(resume_rows(rows, snapshot, old, new)) == 1
    assert len(rows) == 2


def test_legacy_live_profile_matches_the_previously_observed_safe_metadata_hash(db):
    current = public_profile(
        {**execution_profile("gemini-3.5-flash-lite", mode="live"), "free_tier_confirmed": True}
    )
    # Pin the historical notice from HEAD f5f4197; keep the observed hash unchanged.
    historical = {
        **legacy_profile(current),
        "transfer_scope": (
            "선택 범위의 보존 텍스트·코드·diff·문서와 출처 정보, 처리된 "
            "PDF·이미지, 분석 지시를 전송합니다. AI 분석 단계에서는 "
            "개인정보·민감정보·Secret을 자동 탐지·마스킹·가명 처리하지 "
            "않습니다. 수집·Git 원본 조회에는 기존의 제한적인 Secret "
            "검사·민감 경로 제외가 적용됩니다. 제외된 본문은 분석 입력에 포함되지 "
            "않으며 완전한 민감정보 탐지를 보장하지 않습니다. 사용자가 전체 전송 "
            "자료·제외/처리 한계와 제공 권한을 직접 확인해야 합니다. 전체 전송 "
            "재료 JSON과 각 비텍스트 자료를 준비 화면에서 확인·다운로드할 수 "
            "있습니다. 분할·중복 통합된 자료가 단계별 호출에 사용되며 후속 "
            "문맥에는 이번 실행의 AI 응답도 포함됩니다. 제외된 본문과 DOCX "
            "원본은 전송하지 않습니다. DOCX 추출 텍스트·파생 PDF/이미지는 "
            "포함될 수 있습니다."
        ),
        "data_policy": (
            "Google Gemini Developer API의 Unpaid "
            "Services 정책을 적용합니다. 입력·응답은 Google 제품과 "
            "ML 개선에 사용되고 사람이 검토할 수 있습니다. 민감·기밀·개인정보를 "
            "전송하면 안 됩니다. AI 분석 단계의 별도 자동 보호는 제공하지 "
            "않습니다. 사용자가 제공 권한과 PDF·이미지 내부 정보까지 직접 "
            "확인해야 합니다. 사용자 확인이 서비스의 법적·기술적 책임을 모두 "
            "면제하지는 않습니다. Free tier는 운영자 확인이며 실제 "
            "Billing·비용을 증명하지 않습니다. 이미 시작된 외부 호출의 즉시 "
            "중단·과금 취소는 보장하지 않습니다. 정책 확인: "
            "https://ai.google.dev/gemini-api/terms "
            "(안내 버전 2026-10-19)"
        ),
        "notice_version": ("analysis-consent-user-reviewed-collection-safety-2026-10-19"),
    }
    value = db.one(
        "SELECT encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex') AS digest",
        (Jsonb(historical),),
    )
    assert value["digest"] == "575dd70c016a722f2784fcb1b039a6f2b3d130217a46c73e32e5eec8dee300c6"


def seed_failed_legacy_history(db, worker, pid, cid, snapshot):
    # Historical fixture rows only, no model/provider execution to create them.
    request, _ = accepted(worker, pid, cid)
    stored = original(db, request)
    profile = stored["consent"]["descriptor"]["profile"]
    checkpoints = discovery_rows(snapshot, profile, empty_before_last=True)
    owner = uuid4()
    with db.connect() as conn:
        for seq in range(1, len(checkpoints) + 2):
            conn.execute(
                "INSERT INTO analysis_calls(request_id,project_id,generation,owner_token,"
                "seq,phase,payload_digest,outcome) VALUES (%s,%s,%s,%s,%s,%s,%s,'responded')",
                (
                    request,
                    pid,
                    worker.generation,
                    owner,
                    seq,
                    "discover" if seq <= len(checkpoints) else "consolidate",
                    "a" * 64,
                ),
            )
        for row in checkpoints:
            conn.execute(
                "INSERT INTO analysis_checkpoints(request_id,project_id,ordinal,task,"
                "contract_digest,request_digest,response,response_digest,origin_request_id,"
                "origin_call_seq) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request,
                    pid,
                    row["ordinal"],
                    row["task"],
                    row["contract_digest"],
                    row["request_digest"],
                    Jsonb(row["response"]),
                    row["response_digest"],
                    request,
                    row["ordinal"],
                ),
            )
        conn.execute(
            "UPDATE analysis_requests SET state='failed',finished_at=clock_timestamp(),"
            "error_code='candidate_coverage_invalid',call_seq=%s WHERE id=%s",
            (len(checkpoints) + 1, request),
        )
    return request


def exercise_264_checkpoint_chain(db, tmp_path, monkeypatch, *, later_failure=False):
    control = tmp_path / "mode"
    control.write_text("checkpoint_sdk_failure" if later_failure else "checkpoint_sdk_success")
    pid, cid, _, owner, worker, directory = setup_worker(
        db, tmp_path, mode="sdk_checkpoint", control_file=control
    )
    try:
        # Retain a real preparation's collection manifest; replace only fixture input entries.
        initial, _ = accepted(worker, pid, cid)
        record = original(db, initial)
        fixed = decode_snapshot(
            record["input_payload"], record["input_manifest"], record["input_digest"]
        )
        db.execute(
            "UPDATE analysis_requests SET state='failed',finished_at=clock_timestamp() WHERE id=%s",
            (initial,),
        )
        entries = tuple(
            Evidence(
                f"image-{i}",
                "{}",
                media=(
                    Media(f"image-{i}.png", "image/png", b"synthetic-image-" + str(i).encode()),
                ),
            )
            for i in range(263)
        )
        snapshot = ExperienceInput(
            pid,
            fixed.prepared_at,
            fixed.collections_json,
            (*entries, Evidence("text", "{}", "counter fix synthetic evidence")),
        )
        monkeypatch.setattr(
            "project_log.experience_input.ExperienceInputs.prepare",
            lambda *args, **kwargs: snapshot,
        )
        current = worker.profile
        old = legacy_profile(current)
        worker.profile = worker.admission.profile = worker.state.profile = old
        first = seed_failed_legacy_history(db, worker, pid, cid, snapshot)
        prep = prepare_resume(worker, pid, cid, first)
        second = worker.admission.submit(pid, submission(prep))
        second_id = str(second["id"])
        db.execute(
            "UPDATE analysis_requests SET state='failed',finished_at=clock_timestamp(),"
            "error_code='candidate_coverage_invalid',call_seq=1 WHERE id=%s",
            (second_id,),
        )
        before = [original(db, rid) for rid in (first, second_id)]
        old_cp = [read_rows(db, pid, rid) for rid in (first, second_id)]
        assert [len(rows) for rows in old_cp] == [264, 264]
        worker.profile = worker.admission.profile = worker.state.profile = current

        def forbidden(*args, **kwargs):
            pytest.fail(
                "Resume must use the accepted original snapshot, never current source bodies"
            )

        monkeypatch.setattr("project_log.experience_input.ExperienceInputs.prepare", forbidden)
        prep = prepare_resume(worker, pid, cid, second_id)
        assert prep["state"] == "ready", prep
        lineage = prep["descriptor"]["resume"]
        assert lineage["compatibility"]["policy"] == DISCOVERY_COMPATIBILITY
        assert lineage["checkpoint_count"] == 264
        assert lineage["inherited_attempts"] == 266
        assert prep["input_digest"] == snapshot.digest
        preview = worker.admission.transfer(pid, str(prep["id"]), download=True)
        assert preview["input_digest"] == snapshot.digest
        assert "reconcile" in preview["task_instructions"]
        assert preview["consolidation"]["review_window_seeds"] == 64
        assert prep["consent_digest"] != before[-1]["consent"]["consent_digest"]
        accepted_resume = worker.admission.submit(pid, submission(prep))
        copied = read_rows(db, pid, str(accepted_resume["id"]))
        assert len(copied) == 264
        assert all(
            row["contract_digest"] == contract_digest(snapshot.digest, current) for row in copied
        )
        assert [row["origin_request_id"] for row in copied] == [
            row["origin_request_id"] for row in old_cp[-1]
        ]
        worker.start()
        done = terminal(worker, pid, str(accepted_resume["id"]))
        if later_failure:
            assert done["state"] == "failed" and done["call_seq"] == 4, done
            assert not Experiences(db).listing(pid)
            valid = read_rows(db, pid, str(done["id"]))
            assert len(valid) == 267
            assert valid[-2]["task"] == "reconcile" and valid[-1]["task"] == "reconstruct"
            assert json.loads((directory / "quota.json").read_text())["count"] == 4
            control.write_text("checkpoint_sdk_success")
            next_prep = prepare_resume(worker, pid, cid, str(done["id"]))
            next_request = worker.admission.submit(pid, submission(next_prep))
            done = terminal(worker, pid, str(next_request["id"]))
            assert done["state"] == "completed", done
            assert done["inherited_attempts"] == 270
            assert done["call_seq"] == 2
            assert json.loads((directory / "quota.json").read_text())["count"] == 6
        else:
            assert done["state"] == "completed", done
            assert done["inherited_attempts"] == 266
            assert done["call_seq"] == 5
            assert json.loads((directory / "quota.json").read_text())["count"] == 5
        phases = [
            row["phase"]
            for row in db.all(
                "SELECT phase FROM analysis_calls WHERE request_id=%s ORDER BY seq", (done["id"],)
            )
        ]
        assert phases == (
            ["audit", "assess"]
            if later_failure
            else ["consolidate", "reconcile", "reconstruct", "audit", "assess"]
        )
        assert len(Experiences(db).listing(pid)) == 1
        assert [original(db, rid) for rid in (first, second_id)] == before
        assert [read_rows(db, pid, rid) for rid in (first, second_id)] == old_cp
    finally:
        stop(worker, owner)


def test_264_discovery_checkpoints_two_failure_histories_new_consent_and_sdk_quota(
    db, tmp_path, monkeypatch
):
    exercise_264_checkpoint_chain(db, tmp_path, monkeypatch)


def test_later_failure_reuses_global_review_checkpoint_and_counts_failed_sdk_call(
    db, tmp_path, monkeypatch
):
    exercise_264_checkpoint_chain(db, tmp_path, monkeypatch, later_failure=True)


def test_schema012_013_upgrade_preserves_schema011_histories_checkpoints_and_ledger(tmp_path):
    with temporary_database() as dsn:
        db = Database(dsn)
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in sorted(Path("src/project_log/migrations").glob("*.sql"))[:11]:
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (path.name, sha256(path.read_bytes())),
                )
        pid, cid, _, owner, worker, _ = setup_worker(db, tmp_path)
        try:
            worker.profile = worker.admission.profile = worker.state.profile = legacy_profile(
                worker.profile
            )
            first, _ = accepted(worker, pid, cid)
            row = original(db, first)
            snapshot = decode_snapshot(
                row["input_payload"], row["input_manifest"], row["input_digest"]
            )
            db.execute(
                "UPDATE analysis_requests SET state='failed',finished_at=clock_timestamp() "
                "WHERE id=%s",
                (first,),
            )
            request = seed_failed_legacy_history(db, worker, pid, cid, snapshot)
            tables = [
                "projects",
                "collections",
                "user_materials",
                "analysis_preparations",
                "analysis_requests",
                "analysis_calls",
                "analysis_checkpoints",
                "schema_migrations",
            ]
            before = {table: db.all(f"SELECT * FROM {table} ORDER BY 1,2") for table in tables}
            assert read_rows(db, pid, request)
            db.migrate()
            db.migrate()
            for table in tables:
                after = db.all(f"SELECT * FROM {table} ORDER BY 1,2")
                if table == "schema_migrations":
                    assert after[:11] == before[table]
                    assert len(after) == 13
                elif table == "analysis_calls":
                    for row in after:
                        assert row.pop("logical_digest") is None
                    assert after == before[table]
                else:
                    assert after == before[table]
            constraint = db.one(
                "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conname='analysis_checkpoints_task_check'"
            )
            assert "reconcile" in constraint["definition"]
        finally:
            stop(worker, owner)
