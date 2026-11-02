"""Real HTTP admission -> spawned offline Worker -> DB/evidence, temporary resources only."""

import json
from uuid import uuid4

import httpx
import pytest
from analysis_worker_fixtures import offline_factory
from fastapi.testclient import TestClient
from test_analysis_admission import collection
from test_analysis_worker import wait_for
from test_materials import project

from project_log.analysis_execution import execution_profile
from project_log.api import create_app
from project_log.materials import Materials


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Credential/provider access is forbidden")

    monkeypatch.setattr("project_log.gemini.read_operator_key", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "0")
    monkeypatch.delenv("PROJECT_LOG_AI_PREPARATION_ONLY", raising=False)


def setup(db, tmp_path):
    pid = project(db, tmp_path)
    cid = collection(db, pid)
    Materials(db).add(pid, "context.md", b"Synthetic correction evidence")
    control = tmp_path / "control"
    control.mkdir()
    mode = control / "mode"
    mode.write_text("success")
    options = {
        "analysis_profile": {
            **execution_profile("synthetic-offline"),
            "test_directory": str(control),
            "test_control_file": str(mode),
        },
        "analysis_model_factory": offline_factory,
    }
    return pid, cid, control, mode, options


def prepare(client, pid, cid):
    response = client.post(
        f"/api/projects/{pid}/analysis/preparations",
        json={"preparation_id": str(uuid4()), "end_collection_id": cid},
    )
    assert response.status_code == 200, response.text
    return response.json()


def body(prep):
    return {
        "request_id": str(uuid4()),
        "preparation_id": prep["id"],
        "input_digest": prep["input_digest"],
        "consent_digest": prep["consent_digest"],
        "relation_recovery_digest": prep["descriptor"].get("relation_recovery_digest"),
        "relation_transition_digest": prep["descriptor"]
        .get("resume", {})
        .get("transition", {})
        .get("digest"),
        "confirmed": True,
    }


def submit(client, pid, payload):
    response = client.post(f"/api/projects/{pid}/analysis/requests", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def terminal(client, pid, rid, *, timeout=15):
    def read():
        row = client.get(f"/api/projects/{pid}/analysis/requests/{rid}").json()
        return row if row["state"] in {"completed", "failed", "cancelled"} else None

    return wait_for(read, timeout=timeout)


@pytest.mark.parametrize("mode", ["grounding_extra_labels", "grounding_blank_reason"])
def test_changed_error_family_shares_budget_and_blank_reason_never_regenerates(db, tmp_path, mode):
    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text(mode)
    with TestClient(create_app(db, **options)) as client:
        prep = prepare(client, pid, cid)
        assert prep["state"] == "ready"
        assert not db.all("SELECT * FROM analysis_calls")
        request = submit(client, pid, body(prep))
        failed = terminal(client, pid, request["id"])
        assert failed["state"] == "failed" and failed["result_run_id"] is None
        calls = db.all("SELECT * FROM analysis_calls ORDER BY seq")
        relations = [row for row in calls if row["phase"] == "reconcile"]
        if mode == "grounding_extra_labels":
            assert [row["error_code"] for row in relations] == [
                "candidate_relation_grounding_merge_labels_m0_x1_d0",
                "candidate_relation_grounding_split_labels_m0_x1_d0",
            ]
            assert relations[0]["logical_digest"] == relations[1]["logical_digest"]
            assert failed["error_code"] == relations[-1]["error_code"]
            assert failed["resume_block_reason"] == "analysis_relation_attempts_exhausted"
        else:
            assert len(relations) == 1
            assert failed["error_code"] == "candidate_relation_grounding_merge_reason_blank_n1"
            assert failed["resume_block_reason"] == "analysis_resume_validation_required"
        assert [
            row["task"] for row in db.all("SELECT * FROM analysis_checkpoints ORDER BY ordinal")
        ] == [
            "discover",
            "consolidate",
        ]
        assert len(list(control.glob("attempt-*"))) == len(calls)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("grounding_merge_missing", "candidate_relation_grounding_merge_labels_m1_x0_d0"),
        ("grounding_split_duplicate", "candidate_relation_grounding_split_labels_m0_x0_d1"),
        ("grounding_merge_refs", "candidate_relation_grounding_merge_refs_d0_e0_x1_u1"),
        ("grounding_split_refs", "candidate_relation_grounding_split_refs_d0_e0_x1_u1"),
    ],
)
def test_grounding_diagnostic_survives_spawn_and_api_with_bounded_recovery(
    db, tmp_path, mode, code
):
    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text(mode)
    with TestClient(create_app(db, **options)) as client:
        prep = prepare(client, pid, cid)
        assert prep["state"] == "ready"
        assert not db.all("SELECT * FROM analysis_calls")
        assert not list(control.glob("attempt-*"))
        request = submit(client, pid, body(prep))
        failed = terminal(client, pid, request["id"])
        assert failed["state"] == "failed" and failed["error_code"] == code
        assert failed["result_run_id"] is None
        assert failed["resume_block_reason"] == "analysis_relation_attempts_exhausted"
        assert not failed["resume_contract_changed"] and not failed["diagnostic_available"]
        calls = db.all("SELECT * FROM analysis_calls ORDER BY seq")
        assert [c["phase"] for c in calls] == ["discover", "consolidate", "reconcile", "reconcile"]
        assert all(c["outcome"] == "responded" for c in calls)
        assert [c["error_code"] for c in calls[-2:]] == [code, code]
        assert calls[-1]["logical_digest"] == calls[-2]["logical_digest"]
        assert len(list(control.glob("attempt-*"))) == len(calls)
        checkpoints = db.all("SELECT * FROM analysis_checkpoints ORDER BY ordinal")
        assert [c["task"] for c in checkpoints] == ["discover", "consolidate"]
        blocked = client.post(
            f"/api/projects/{pid}/analysis/preparations",
            json={
                "preparation_id": str(uuid4()),
                "end_collection_id": cid,
                "resume_from_request_id": request["id"],
            },
        ).json()
        assert blocked["state"] == "failed"
        assert blocked["error_code"] == "analysis_relation_attempts_exhausted"
        assert db.all("SELECT * FROM analysis_calls ORDER BY seq") == calls
        assert db.all("SELECT * FROM analysis_checkpoints ORDER BY ordinal") == checkpoints
        assert not db.all("SELECT * FROM experience_runs")
        # A historical unclassified failure remains blocked under this identical contract.
        db.execute(
            "UPDATE analysis_requests SET error_code=%s WHERE id=%s",
            ("candidate_relation_grounding_invalid", request["id"]),
        )
        historical = client.get(f"/api/projects/{pid}/analysis/requests/{request['id']}").json()
        assert historical["resume_block_reason"] == "analysis_resume_validation_required"
        assert not historical["resume_contract_changed"] and not historical["diagnostic_available"]


@pytest.mark.parametrize(
    "mode", ["grounding_merge_once", "grounding_split_once", "grounding_sdk_once"]
)
def test_grounding_regeneration_is_consented_bounded_and_checkpointed(
    db, tmp_path, monkeypatch, mode
):
    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text(mode)
    if mode == "grounding_sdk_once":
        options["analysis_profile"]["model"] = "gemini-3.5-flash-lite"
    app = create_app(db, **options)
    invalid_saves = []
    with TestClient(app) as client:
        if mode == "grounding_sdk_once":
            from project_log.git import CollectionError

            worker = app.state.analysis_worker
            annotate = worker.state.relation_failed

            def checked_annotation(project, request, owner, seq, code):
                annotate(project, request, owner, seq, code)
                try:
                    worker.state.checkpoint(
                        project,
                        request,
                        owner,
                        {
                            "ordinal": 3,
                            "task": "reconcile",
                            "request_digest": "a" * 64,
                            "response": {"assignments": {}, "grounds": [], "splits": []},
                        },
                    )
                except CollectionError as exc:
                    invalid_saves.append(exc.code)
                else:
                    invalid_saves.append("unexpected_invalid_save")

            monkeypatch.setattr(worker.state, "relation_failed", checked_annotation)
        prep = prepare(client, pid, cid)
        preview = client.get(
            f"/api/projects/{pid}/analysis/preparations/{prep['id']}/transfer"
        ).json()
        assert preview["relation_recovery"]["max_attempts"] == 2
        assert preview["relation_recovery"]["raw_response_retransmitted"] is False
        assert preview["transfer_digest"] == prep["descriptor"]["transfer_digest"]
        assert not db.all("SELECT * FROM analysis_calls")
        invalid_consent = {**body(prep), "consent_digest": "0" * 64}
        rejected = client.post(
            f"/api/projects/{pid}/analysis/requests", json=invalid_consent
        ).json()
        assert rejected["state"] == "rejected"
        assert not db.all("SELECT * FROM analysis_calls")
        # A stale UI can read a new consent digest but cannot silently opt into
        # conditional transmission without the explicit recovery-policy ACK.
        stale = body(prep)
        stale.pop("relation_recovery_digest")
        rejected = client.post(f"/api/projects/{pid}/analysis/requests", json=stale).json()
        assert (
            rejected["state"] == "rejected"
            and rejected["error_code"] == "analysis_consent_mismatch"
        )
        assert not db.all("SELECT * FROM analysis_calls")
        row = submit(client, pid, body(prep))
        done = terminal(client, pid, row["id"])
        assert done["state"] == "completed", done
        calls = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (row["id"],)
        )
        relations = [c for c in calls if c["phase"] == "reconcile"]
        assert len(relations) == 2 and relations[0]["error_code"].startswith(
            "candidate_relation_grounding_"
        )
        assert relations[1]["error_code"] is None
        assert relations[0]["payload_digest"] != relations[1]["payload_digest"]
        assert relations[0]["logical_digest"] == relations[1]["logical_digest"]
        cp = db.all(
            "SELECT * FROM analysis_checkpoints WHERE request_id=%s AND task='reconcile'",
            (row["id"],),
        )
        assert len(cp) == 1 and cp[0]["origin_call_seq"] == relations[1]["seq"]
        assert "private" not in json.dumps(cp[0]["response"])
        if mode == "grounding_sdk_once":
            assert invalid_saves == ["analysis_checkpoint_invalid"]
            first, second = [
                json.loads((control / f"relation-wire-{i}.json").read_text()) for i in (1, 2)
            ]
            original = json.loads(json.loads(first["contents"][0]["parts"][0]["text"])["context"])
            feedback = json.loads(json.loads(second["contents"][0]["parts"][0]["text"])["context"])
            assert feedback.pop("relation_feedback")["code"] == relations[0]["error_code"]
            assert feedback == original
            assert "private-response" not in json.dumps(
                second
            ) and "private-outside" not in json.dumps(second)
            assert json.loads((control / "quota.json").read_text())["count"] == len(calls)


@pytest.mark.parametrize(
    "mode,last",
    [
        ("grounding_merge_then_503", "provider_temporarily_unavailable"),
        ("grounding_merge_then_429", "provider_minute_limit"),
        ("grounding_503_first", "candidate_relation_grounding_merge_refs_d0_e0_x1_u1"),
    ],
)
def test_grounding_feedback_and_transport_share_cap_even_in_independent_new_run(
    db, tmp_path, mode, last
):
    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text(mode)
    with TestClient(create_app(db, **options)) as client:
        row = submit(client, pid, body(prepare(client, pid, cid)))
        failed = terminal(client, pid, row["id"], timeout=45)
        assert failed["error_code"] == last
        assert failed["resume_block_reason"] == "analysis_relation_attempts_exhausted"
        calls = db.all("SELECT * FROM analysis_calls WHERE phase='reconcile' ORDER BY entered_at")
        assert (
            len(calls) == 2
            and calls[0]["outcome"] == ("failed" if mode == "grounding_503_first" else "responded")
            and calls[1]["outcome"] == ("responded" if mode == "grounding_503_first" else "failed")
        )
        # Same input without a Resume parent cannot create a third attempt.
        another = submit(client, pid, body(prepare(client, pid, cid)))
        stopped = terminal(client, pid, another["id"])
        assert stopped["error_code"] == "analysis_relation_attempts_exhausted"
        assert (
            db.all("SELECT * FROM analysis_calls WHERE phase='reconcile' ORDER BY entered_at")
            == calls
        )

    db.execute("UPDATE analysis_calls SET entered_at=entered_at-interval '2 days'")
    with TestClient(create_app(db, **options)) as client:
        historical = client.get(f"/api/projects/{pid}/analysis/requests/{row['id']}").json()
        assert historical["resume_block_reason"] == "analysis_relation_attempts_exhausted"
        third = submit(client, pid, body(prepare(client, pid, cid)))
        assert (
            terminal(client, pid, third["id"])["error_code"]
            == "analysis_relation_attempts_exhausted"
        )
        assert len(db.all("SELECT * FROM analysis_calls WHERE phase='reconcile'")) == 2


@pytest.mark.parametrize(
    "stop_code",
    [
        "analysis_budget_exceeded",
        "gemini_free_daily_limit",
        "failed_server_interrupted",
        "unknown_annotation",
        "cancelled",
    ],
)
def test_remaining_feedback_survives_quota_day_run_and_server_restart(
    db, tmp_path, monkeypatch, stop_code
):
    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text("grounding_merge_once")
    app = create_app(db, **options)
    with TestClient(app) as client:
        worker = app.state.analysis_worker
        enter = worker.state.enter_call

        def deny_feedback(
            project,
            request,
            owner,
            seq,
            phase,
            digest,
            variant=None,
            feedback_code=None,
            logical_digest=None,
        ):
            if variant == "feedback":
                from project_log.git import CollectionError

                if stop_code == "cancelled":
                    worker.state.cancel(project, request)
                actual = (
                    "failed_server_interrupted"
                    if stop_code == "unknown_annotation"
                    else ("analysis_stopped" if stop_code == "cancelled" else stop_code)
                )
                raise CollectionError(actual, "Synthetic stop before HTTP entry")
            return enter(
                project, request, owner, seq, phase, digest, variant, feedback_code, logical_digest
            )

        monkeypatch.setattr(worker.state, "enter_call", deny_feedback)
        row = submit(client, pid, body(prepare(client, pid, cid)))
        stopped = terminal(client, pid, row["id"])
        if stop_code == "cancelled":
            assert stopped["state"] == "cancelled"
        else:
            assert stopped["error_code"] == (
                "failed_server_interrupted" if stop_code == "unknown_annotation" else stop_code
            )
        calls = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (row["id"],)
        )
        assert (
            len(calls) == 3
            and calls[-1]["error_code"] == "candidate_relation_grounding_merge_labels_m1_x0_d0"
        )
        assert len(db.all("SELECT * FROM analysis_checkpoints")) == 2
        if stop_code == "gemini_free_daily_limit":
            assert stopped["resume_block_reason"] == "analysis_resume_daily_wait"
    db.execute(
        "UPDATE analysis_requests SET finished_at=finished_at-interval '2 days' WHERE id=%s",
        (row["id"],),
    )
    if stop_code == "unknown_annotation":
        db.execute(
            "UPDATE analysis_calls SET error_code=NULL WHERE request_id=%s AND phase='reconcile'",
            (row["id"],),
        )
    before = db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (row["id"],))
    with TestClient(create_app(db, **options)) as client:
        prepared = client.post(
            f"/api/projects/{pid}/analysis/preparations",
            json={
                "preparation_id": str(uuid4()),
                "end_collection_id": cid,
                "resume_from_request_id": row["id"],
            },
        ).json()
        if stop_code in {"unknown_annotation", "cancelled"}:
            assert prepared["state"] == "failed"
            assert prepared["error_code"] == (
                "analysis_relation_budget_unknown"
                if stop_code == "unknown_annotation"
                else "analysis_resume_unavailable"
            )
            assert (
                db.all(
                    "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (row["id"],)
                )
                == before
            )
            return
        assert prepared["state"] == "ready", prepared
        assert prepared["descriptor"]["resume"]["checkpoint_count"] == 2
        child = submit(client, pid, body(prepared))
        done = terminal(client, pid, child["id"])
        assert done["state"] == "completed", done
        newer = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (child["id"],)
        )
        assert [c["phase"] for c in newer] == ["reconcile", "reconstruct", "audit", "assess"]
        assert newer[0]["logical_digest"] == before[-1]["logical_digest"]
        assert (
            db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (row["id"],))
            == before
        )
        cp = db.one(
            "SELECT * FROM analysis_checkpoints WHERE request_id=%s AND task='reconcile'",
            (child["id"],),
        )
        assert cp["origin_call_seq"] == 1 and str(cp["origin_request_id"]) == child["id"]


def test_valid_regenerated_checkpoint_replays_without_new_reconcile_or_quota_entry(
    db, tmp_path, monkeypatch
):
    from project_log.git import CollectionError

    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text("grounding_merge_once")
    app = create_app(db, **options)
    with TestClient(app) as client:
        worker = app.state.analysis_worker
        enter = worker.state.enter_call

        def stop_before_audit(
            project,
            request,
            owner,
            seq,
            phase,
            digest,
            variant=None,
            feedback_code=None,
            logical_digest=None,
        ):
            if phase == "audit":
                raise CollectionError("provider_network_error", "Synthetic interruption")
            return enter(
                project, request, owner, seq, phase, digest, variant, feedback_code, logical_digest
            )

        monkeypatch.setattr(worker.state, "enter_call", stop_before_audit)
        row = submit(client, pid, body(prepare(client, pid, cid)))
        assert terminal(client, pid, row["id"])["error_code"] == "provider_network_error"
    original = db.all(
        "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal", (row["id"],)
    )
    assert [c["task"] for c in original] == ["discover", "consolidate", "reconcile", "reconstruct"]
    with TestClient(create_app(db, **options)) as client:
        prepared = client.post(
            f"/api/projects/{pid}/analysis/preparations",
            json={
                "preparation_id": str(uuid4()),
                "end_collection_id": cid,
                "resume_from_request_id": row["id"],
            },
        ).json()
        assert prepared["state"] == "ready", prepared
        child = submit(client, pid, body(prepared))
        assert terminal(client, pid, child["id"])["state"] == "completed"
        assert [
            c["phase"]
            for c in db.all(
                "SELECT phase FROM analysis_calls WHERE request_id=%s ORDER BY seq", (child["id"],)
            )
        ] == ["audit", "assess"]
        copied = db.all(
            "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal LIMIT 4",
            (child["id"],),
        )
        assert all(
            {k: v for k, v in a.items() if k != "request_id"}
            == {k: v for k, v in b.items() if k != "request_id"}
            for a, b in zip(original, copied, strict=True)
        )


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "independent", "independent_cancelled", "unknown"]
)
def test_reviewed_contract_transition_preserves_spent_history_and_new_shared_cap(
    db, tmp_path, outcome
):
    from psycopg.types.json import Jsonb

    from project_log.analysis_checkpoints import PREVIOUS_RELATION_INSTRUCTION, contract_digest

    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text("grounding_extra_labels")
    with TestClient(create_app(db, **options)) as client:
        original = submit(client, pid, body(prepare(client, pid, cid)))
        assert terminal(client, pid, original["id"])["call_seq"] == 4
    source = db.one("SELECT * FROM analysis_requests WHERE id=%s", (original["id"],))
    # Only historical metadata is rebound in the disposable DB. This does not
    # claim that the prior compiler ran. Its two known attempts remain spent.
    consent = source["consent"]
    old = {**consent["descriptor"]["profile"], "instruction_digest": PREVIOUS_RELATION_INSTRUCTION}
    consent["descriptor"]["profile"] = old
    db.execute(
        "UPDATE analysis_requests SET consent=%s WHERE id=%s", (Jsonb(consent), original["id"])
    )
    db.execute(
        "UPDATE analysis_checkpoints SET contract_digest=%s WHERE request_id=%s",
        (contract_digest(source["input_digest"], old), original["id"]),
    )
    db.execute(
        "UPDATE analysis_calls SET logical_digest=%s WHERE request_id=%s AND phase='reconcile'",
        ("e" * 64, original["id"]),
    )
    if outcome == "unknown":
        db.execute(
            "UPDATE analysis_calls SET error_code=NULL WHERE request_id=%s AND seq=4",
            (original["id"],),
        )
    frozen = db.all(
        "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal", (original["id"],)
    )
    ledger = db.all(
        "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (original["id"],)
    )
    mode_file.write_text(
        "grounding_merge_once" if outcome == "success" else "grounding_extra_labels"
    )
    with TestClient(create_app(db, **options)) as client:
        view = client.get(f"/api/projects/{pid}/analysis/requests/{original['id']}").json()
        if outcome == "unknown":
            assert view["resume_block_reason"] == "analysis_relation_budget_unknown"
            failed = client.post(
                f"/api/projects/{pid}/analysis/preparations",
                json={
                    "preparation_id": str(uuid4()),
                    "end_collection_id": cid,
                    "resume_from_request_id": original["id"],
                },
            ).json()
            assert failed["error_code"] == "analysis_relation_budget_unknown"
            assert db.all("SELECT * FROM analysis_calls ORDER BY seq") == ledger
            return
        assert view["resume_block_reason"] is None and not view["diagnostic_available"]
        preparations = []
        for _ in range(2):
            p = client.post(
                f"/api/projects/{pid}/analysis/preparations",
                json={
                    "preparation_id": str(uuid4()),
                    "end_collection_id": cid,
                    "resume_from_request_id": original["id"],
                },
            ).json()
            assert p["state"] == "ready", p
            preparations.append(p)
        first, sibling = preparations
        transition = first["descriptor"]["resume"]["transition"]
        assert transition["previous_attempts"] == transition["max_attempts"] == 2
        assert (
            transition["max_regenerations"] == 1
            and not transition["old_response_or_feedback_reused"]
        )
        assert transition["source_contract"] != transition["target_contract"]
        assert (
            first["descriptor"]["resume"]["compatibility"]["reused_stage"] == "discover_consolidate"
        )
        preview = client.get(
            f"/api/projects/{pid}/analysis/preparations/{first['id']}/transfer"
        ).json()
        assert preview["relation_transition"] == transition
        assert preview["transfer_digest"] == first["descriptor"]["transfer_digest"]
        for value in [None, "0" * 64]:
            payload = {**body(first), "relation_transition_digest": value}
            rejected = client.post(f"/api/projects/{pid}/analysis/requests", json=payload).json()
            assert (
                rejected["state"] == "rejected"
                and rejected["error_code"] == "analysis_consent_mismatch"
            )
        assert db.all("SELECT * FROM analysis_calls ORDER BY seq") == ledger
        if outcome.startswith("independent"):
            target = submit(client, pid, body(prepare(client, pid, cid)))
            terminal(client, pid, target["id"])
            if outcome == "independent_cancelled":
                db.execute(
                    "UPDATE analysis_requests SET state='cancelled' WHERE id=%s", (target["id"],)
                )
            replay = client.post(f"/api/projects/{pid}/analysis/requests", json=body(first)).json()
            assert (
                replay["state"] == "rejected"
                and replay["error_code"] == "analysis_relation_transition_consumed"
            )
            assert (
                client.get(f"/api/projects/{pid}/analysis/requests/{original['id']}").json()[
                    "resume_block_reason"
                ]
                == "analysis_relation_transition_consumed"
            )
        else:
            target = submit(client, pid, body(first))
            done = terminal(client, pid, target["id"])
            assert done["state"] == ("completed" if outcome == "success" else "failed"), done
            assert done["inherited_attempts"] == 4 and done["reused_checkpoints"] == 2
            fresh = db.all(
                "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (target["id"],)
            )
            relations = [c for c in fresh if c["phase"] == "reconcile"]
            assert len(relations) == 2
            assert relations[0]["logical_digest"] == relations[1]["logical_digest"] != "e" * 64
            if outcome == "success":
                # A carried old feedback would have made this fixture succeed at once.
                assert (
                    relations[0]["error_code"]
                    == "candidate_relation_grounding_merge_labels_m1_x0_d0"
                )
                cp = db.one(
                    "SELECT * FROM analysis_checkpoints WHERE request_id=%s AND task='reconcile'",
                    (target["id"],),
                )
                assert cp["origin_call_seq"] == 2
            else:
                assert done["resume_block_reason"] == "analysis_relation_attempts_exhausted"
            duplicate = client.post(
                f"/api/projects/{pid}/analysis/requests", json=body(sibling)
            ).json()
            assert (
                duplicate["state"] == "rejected"
                and duplicate["error_code"] == "analysis_resume_consumed"
            )
        assert (
            db.all(
                "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal",
                (original["id"],),
            )
            == frozen
        )
        assert (
            db.all(
                "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (original["id"],)
            )
            == ledger
        )
    if outcome in {"failure", "independent", "independent_cancelled"}:
        # Acceptance and new work limits survive server restart and date rollover.
        db.execute(
            "UPDATE analysis_requests SET finished_at=finished_at-interval '2 days' WHERE id=%s",
            (target["id"],),
        )
        with TestClient(create_app(db, **options)) as client:
            view = client.get(f"/api/projects/{pid}/analysis/requests/{target['id']}").json()
            if outcome == "independent_cancelled":
                assert view["state"] == "cancelled"
            else:
                assert view["resume_block_reason"] == "analysis_relation_attempts_exhausted"
            if outcome.startswith("independent"):
                assert (
                    client.get(f"/api/projects/{pid}/analysis/requests/{original['id']}").json()[
                        "resume_block_reason"
                    ]
                    == "analysis_relation_transition_consumed"
                )


def test_previous_grounding_instructions_cannot_resume_after_the_contract_fix(db, tmp_path):
    from psycopg.types.json import Jsonb

    from project_log.analysis_checkpoints import PREVIOUS_RELATION_INSTRUCTION, contract_digest

    pid, cid, control, mode_file, options = setup(db, tmp_path)
    Materials(db).add(pid, "second.md", b"Another synthetic correction")
    mode_file.write_text("grounding_merge_missing")
    with TestClient(create_app(db, **options)) as client:
        request = submit(client, pid, body(prepare(client, pid, cid)))
        terminal(client, pid, request["id"])
    source = db.one("SELECT * FROM analysis_requests WHERE id=%s", (request["id"],))
    # Historical instruction metadata in this disposable DB only. Rebinding
    # provenance does not claim that the old adapter ran. Its spent budget stays intact.
    consent = source["consent"]
    old = dict(consent["descriptor"]["profile"])
    old["instruction_digest"] = PREVIOUS_RELATION_INSTRUCTION
    consent["descriptor"]["profile"] = old
    db.execute(
        "UPDATE analysis_requests SET consent=%s WHERE id=%s",
        (Jsonb(consent), request["id"]),
    )
    prep = db.one(
        "SELECT descriptor FROM analysis_preparations WHERE id=%s", (source["preparation_id"],)
    )
    prep["descriptor"]["profile"] = old
    db.execute(
        "UPDATE analysis_preparations SET descriptor=%s WHERE id=%s",
        (Jsonb(prep["descriptor"]), source["preparation_id"]),
    )
    db.execute(
        "UPDATE analysis_checkpoints SET contract_digest=%s WHERE request_id=%s",
        (contract_digest(source["input_digest"], old), request["id"]),
    )
    frozen = db.all(
        "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal", (request["id"],)
    )
    ledger = db.all(
        "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (request["id"],)
    )
    mode_file.write_text("grounding_merge_once")
    with TestClient(create_app(db, **options)) as client:
        view = client.get(f"/api/projects/{pid}/analysis/requests/{request['id']}").json()
        assert view["resume_block_reason"] == "analysis_resume_validation_required"
        assert not view["diagnostic_available"]
        prepared = client.post(
            f"/api/projects/{pid}/analysis/preparations",
            json={
                "preparation_id": str(uuid4()),
                "end_collection_id": cid,
                "resume_from_request_id": request["id"],
            },
        ).json()
        assert prepared["state"] == "failed", prepared
        assert prepared["error_code"] == "analysis_resume_validation_required"
        assert not db.all(
            "SELECT * FROM analysis_requests WHERE resume_from_request_id=%s", (request["id"],)
        )
        assert (
            db.all(
                "SELECT * FROM analysis_checkpoints WHERE request_id=%s ORDER BY ordinal",
                (request["id"],),
            )
            == frozen
        )
        assert (
            db.all(
                "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (request["id"],)
            )
            == ledger
        )


def test_http_success_frozen_scope_replay_and_history(db, tmp_path):
    pid, cid, control, mode, options = setup(db, tmp_path)
    with TestClient(create_app(db, **options)) as client:
        prep = prepare(client, pid, cid)
        assert prep["state"] == "ready"
        assert prep["descriptor"]["profile"]["provider"].startswith("격리")
        assert "Synthetic correction evidence" not in str(prep)
        payload = body(prep)
        row = submit(client, pid, payload)
        result = terminal(client, pid, row["id"])
        assert result["state"] == "completed"
        assert submit(client, pid, payload)["result_run_id"] == result["result_run_id"]
        runs = client.get(f"/api/projects/{pid}/experiences").json()["runs"]
        assert len(runs) == 1 and runs[0]["id"] == result["result_run_id"]
        detail = client.get(f"/api/projects/{pid}/experiences/{runs[0]['id']}").json()
        assert detail["input_digest"] == prep["input_digest"]
        evidence = client.get(
            f"/api/projects/{pid}/experiences/{runs[0]['id']}/evidence",
            params={"reference": detail["input_manifest"]["entries"][0]["reference"]},
        )
        assert (
            evidence.status_code == 200
            and evidence.json()["body"] == "Synthetic correction evidence"
        )
        assert result["scope"]["collection_ids"] == [cid]
        assert "input_payload" not in result and "owner_token" not in result
        assert (
            client.get(f"/api/projects/{uuid4()}/analysis/requests/{row['id']}").status_code == 404
        )
        assert (
            client.post(
                f"/api/projects/{pid}/analysis/requests", json={**payload, "confirmed": False}
            ).status_code
            == 409
        )
        mode.write_text("empty")
        empty = submit(client, pid, body(prepare(client, pid, cid)))
        assert terminal(client, pid, empty["id"])["state"] == "completed"
        runs = client.get(f"/api/projects/{pid}/experiences").json()["runs"]
        assert len(runs) == 2 and runs[0]["candidate_count"] == 0


def test_http_input_change_capacity_cancel_and_collection_independence(db, tmp_path):
    pid, cid, control, mode, options = setup(db, tmp_path)
    other = project(db, tmp_path, "other")
    other_cid = collection(db, other)
    Materials(db).add(other, "other.md", b"Other synthetic evidence")
    with TestClient(create_app(db, **options)) as client:
        stale = prepare(client, pid, cid)
        Materials(db).add(pid, "new.md", b"New synthetic evidence")
        assert (
            client.get(f"/api/projects/{pid}/analysis/preparations/{stale['id']}").json()["state"]
            == "stale"
        )
        assert submit(client, pid, body(stale))["state"] == "rejected"
        other_prep = prepare(client, other, other_cid)
        mode.write_text("blocked")
        row = submit(client, pid, body(prepare(client, pid, cid)))
        wait_for(lambda: (control / "attempt-1").exists())
        assert client.get(f"/api/projects/{pid}").json()["analysis_active"] is True
        assert client.get(f"/api/projects/{other}").json()["analysis_active"] is False
        refused = submit(client, other, body(other_prep))
        assert refused["state"] == "rejected" and refused["error_code"] == "analysis_capacity_full"
        assert client.get(f"/api/projects/{other}/analysis").json()["capacity"]["state"] == "busy"
        assert (
            client.put(
                f"/api/projects/{pid}/materials",
                content=b"blocked",
                headers={"Content-Type": "application/octet-stream", "X-File-Name": "blocked.md"},
            ).status_code
            == 409
        )
        # New completed Collections are allowed and never added to the admitted scope.
        later = collection(db, pid)
        assert (
            later
            not in client.get(f"/api/projects/{pid}/analysis/requests/{row['id']}").json()["scope"][
                "collection_ids"
            ]
        )
        response = client.post(f"/api/projects/{pid}/analysis/requests/{row['id']}/cancel", json={})
        assert response.json()["state"] in {"cancel_requested", "cancelled"}
        assert terminal(client, pid, row["id"])["state"] == "cancelled"
        assert not client.get(f"/api/projects/{pid}").json()["analysis_active"]
        assert client.get(f"/api/projects/{pid}/experiences").json()["runs"] == []


def test_http_failure_shutdown_restart_preserve_success(db, tmp_path):
    pid, cid, control, mode, options = setup(db, tmp_path)
    app = create_app(db, **options)
    with TestClient(app) as client:
        success = terminal(client, pid, submit(client, pid, body(prepare(client, pid, cid)))["id"])
        mode.write_text("network")
        failed = terminal(client, pid, submit(client, pid, body(prepare(client, pid, cid)))["id"])
        assert failed["state"] == "failed" and failed["error_code"] == "provider_network_error"
        mode.write_text("blocked")
        active = submit(client, pid, body(prepare(client, pid, cid)))
        wait_for(
            lambda: (
                client.get(f"/api/projects/{pid}/analysis/requests/{active['id']}").json()["state"]
                == "running"
            )
        )
    with TestClient(create_app(db, **options)) as client:
        assert (
            client.get(f"/api/projects/{pid}/analysis/requests/{active['id']}").json()["state"]
            == "failed"
        )
        assert not client.get(f"/api/projects/{pid}").json()["analysis_active"]
        assert [r["id"] for r in client.get(f"/api/projects/{pid}/experiences").json()["runs"]] == [
            success["result_run_id"]
        ]
        assert (
            client.get(f"/api/projects/{pid}/analysis").json()["capacity"]["state"] == "available"
        )


def test_http_disabled_is_read_only(db, tmp_path):
    pid = project(db, tmp_path)
    cid = collection(db, pid)
    with TestClient(create_app(db)) as client:
        status = client.get(f"/api/projects/{pid}/analysis").json()
        assert status["available"] is False and status["executions"] == []
        response = client.post(
            f"/api/projects/{pid}/analysis/preparations",
            json={"preparation_id": str(uuid4()), "end_collection_id": cid},
        )
        assert response.status_code == 503
        assert db.all("SELECT id FROM analysis_preparations") == []


@pytest.mark.parametrize("model_id", ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
def test_live_configuration_checks_metadata_without_reading_key(tmp_path, monkeypatch, model_id):
    from project_log.analysis_api import configured_profile

    directory = tmp_path / ".project-log-secrets"
    directory.mkdir(mode=0o700)
    path = directory / "synthetic-key-placeholder"
    path.write_text("not-a-real-credential")
    path.chmod(0o600)
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", model_id)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(path))
    profile = configured_profile()
    assert profile is not None and profile["mode"] == "live"
    assert profile["model"] == model_id
    assert str(path) not in str(profile) and "not-a-real-credential" not in str(profile)
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "0")
    assert configured_profile() is None
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "0")
    assert configured_profile() is None
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "unreviewed-model")
    assert configured_profile() is None
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", model_id)
    path.chmod(0o200)
    assert configured_profile() is None
    path.chmod(0o644)
    assert configured_profile() is None
    path.chmod(0o600)
    link = directory / "link"
    link.symlink_to(path)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(link))
    assert configured_profile() is None


def test_live_35_profile_prepares_synthetic_input_without_key_read_or_model_call(
    db,
    tmp_path,
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Local preparation must not construct a Gemini model")

    monkeypatch.setattr("project_log.experience_gemini.ChatGoogleGenerativeAI", forbidden)
    directory = tmp_path / ".project-log-secrets"
    directory.mkdir(mode=0o700)
    path = directory / "synthetic-key-placeholder"
    path.write_text("not-a-real-credential")
    path.chmod(0o600)
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_KEY_FILE", str(path))
    pid = project(db, tmp_path)
    cid = collection(db, pid)
    Materials(db).add(pid, "context.md", b"Synthetic local preparation evidence")
    with TestClient(create_app(db)) as client:
        assert client.get(f"/api/projects/{pid}/analysis").json()["available"] is True
        prepared = prepare(client, pid, cid)
        assert prepared["state"] == "ready"
        profile = prepared["descriptor"]["profile"]
        assert profile["model"] == "gemini-3.5-flash-lite"
        assert profile["provider"] == "Google Gemini Developer API"
        assert profile["mode"] == "live"
        assert len(prepared["input_digest"]) == len(prepared["consent_digest"]) == 64
        assert str(path) not in str(prepared) and "not-a-real-credential" not in str(prepared)
        assert "Synthetic local preparation evidence" not in str(prepared)
        assert len(db.all("SELECT id FROM analysis_preparations")) == 1
        for table in ("analysis_requests", "analysis_calls", "experience_runs"):
            assert db.one(f"SELECT count(*) AS total FROM {table}")["total"] == 0
        assert client.get(f"/api/projects/{pid}/analysis").json()["executions"] == []


def test_preparation_only_needs_no_key_or_free_confirmation_and_blocks_execution(
    db,
    tmp_path,
    monkeypatch,
):
    from project_log.analysis_admission import SubmitAnalysis
    from project_log.analysis_api import configured_profile
    from project_log.git import CollectionError

    monkeypatch.setenv("PROJECT_LOG_AI_PREPARATION_ONLY", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.delenv("PROJECT_LOG_GEMINI_KEY_FILE", raising=False)
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "0")
    profile = configured_profile()
    assert profile and profile["preparation_only"] and not profile["free_tier_confirmed"]
    # Preparation-only wins even over mistakenly inherited live opt-in flags.
    monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "1")
    monkeypatch.setenv("PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED", "1")
    assert configured_profile() == profile
    pid = project(db, tmp_path)
    cid = collection(db, pid)
    Materials(db).add(pid, "context.md", b"Synthetic preparation-only evidence")
    app = create_app(db)
    with TestClient(app) as client:
        status = client.get(f"/api/projects/{pid}/analysis").json()
        assert status["available"] and not status["execution_available"]
        prepared = prepare(client, pid, cid)
        assert prepared["state"] == "ready"
        assert prepared["descriptor"]["profile"] == profile
        payload = body(prepared)
        response = client.post(f"/api/projects/{pid}/analysis/requests", json=payload)
        assert response.status_code == 422
        assert response.json()["code"] == "analysis_transfer_disabled"
        worker = app.state.analysis_worker
        assert worker.child is None and worker.state.claim() is None
        with pytest.raises(CollectionError, match="로컬 준비 전용"):
            worker.admission.submit(pid, SubmitAnalysis(**payload))
        with pytest.raises(CollectionError, match="로컬 준비 전용"):
            worker.spawn({})
        with pytest.raises(CollectionError, match="로컬 준비 전용"):
            worker.state.enter_call(
                pid, payload["request_id"], str(uuid4()), 1, "discover", "a" * 64
            )
        for table in ("analysis_requests", "analysis_calls", "experience_runs"):
            assert db.one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
        assert not client.get(f"/api/projects/{pid}/experiences").json()["transfer_enabled"]
        monkeypatch.setenv("PROJECT_LOG_AI_PREPARATION_ONLY", "0")
        monkeypatch.setenv("PROJECT_LOG_AI_ENABLED", "0")
        assert not client.get(f"/api/projects/{pid}/analysis").json()["available"]
