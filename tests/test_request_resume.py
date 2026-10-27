"""Normal analysis after a synthetic historical400/consumed diagnostic, never live Gemini."""

import copy
import json

import pytest
from psycopg.types.json import Jsonb
from test_analysis_admission import submission
from test_analysis_diagnostic import diagnostic_factory, diagnostic_setup, prepare_diagnostic
from test_analysis_resume import original, prepare_resume
from test_analysis_worker import stop, terminal

from project_log.analysis_api import request_view
from project_log.analysis_checkpoints import contract_digest, read_rows
from project_log.analysis_execution import execution_profile
from project_log.analysis_input import canonical
from project_log.analysis_recovery import changed_request_resume, resume_block
from project_log.analysis_worker import AnalysisWorker
from project_log.experience_store import Experiences
from project_log.policy import sha256
from project_log.worker import AppOwnership


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("change", ["none", "same", "forged", "model", "other_failure"])
def test_only_reviewed_old400_to_new_request_contract_is_eligible(change, diagnostic):
    profile = execution_profile("synthetic")
    profile.pop(
        "relation_recovery"
    )  # This fixture exercises the prior request-contract transition.
    old = {k: v for k, v in profile.items() if k not in {"request_contract", "relation_recovery"}}
    row = {
        "error_code": "provider_invalid_request",
        "consent": {
            "descriptor": {
                "profile": old,
                "diagnostic": {"historical": True},
            }
        },
    }
    if not diagnostic:
        row["consent"]["descriptor"].pop("diagnostic")
    if change == "same":
        row["consent"]["descriptor"]["profile"] = profile
    elif change == "forged":
        profile["request_contract"] = {"policy": "unreviewed"}
    elif change == "model":
        profile["model"] = "different"
    elif change == "other_failure":
        row["error_code"] = "provider_read_timeout_transport_hunknown"
    assert changed_request_resume(row, profile) == (change == "none")
    allowed = change == "none" or (change == "other_failure" and not diagnostic)
    assert (resume_block(None, row, profile) is None) == allowed


def historical_fixture_profiles(db, requests, profile):
    """Model a recorded pre-request-contract prefix in this disposable DB.

    Discovery/consolidation logical requests and responses are unchanged. Fixture
    construction rebinds that prefix to the old profile; it is not a production
    history rewrite or a claim that the old live compiler was exercised here.
    """
    old = {k: v for k, v in profile.items() if k not in {"request_contract", "relation_recovery"}}
    with db.connect() as conn:
        for rid in requests:
            row = conn.execute("SELECT * FROM analysis_requests WHERE id=%s", (rid,)).fetchone()
            prep = conn.execute(
                "SELECT * FROM analysis_preparations WHERE id=%s", (row["preparation_id"],)
            ).fetchone()
            descriptor = copy.deepcopy(row["consent"]["descriptor"])
            descriptor["profile"] = old
            descriptor.pop("relation_recovery_digest", None)
            digest = sha256(
                canonical(
                    {
                        "project_id": str(row["project_id"]),
                        "preparation_id": str(prep["id"]),
                        "input_digest": row["input_digest"],
                        "source_fingerprint": prep["source_fingerprint"],
                        "descriptor": descriptor,
                    }
                ).encode()
            )
            conn.execute(
                "UPDATE analysis_requests SET consent=%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            k: v
                            for k, v in {
                                **row["consent"],
                                "descriptor": descriptor,
                                "consent_digest": digest,
                            }.items()
                            if k != "relation_recovery_digest"
                        }
                    ),
                    rid,
                ),
            )
            conn.execute(
                "UPDATE analysis_preparations SET descriptor=%s,consent_digest=%s WHERE id=%s",
                (Jsonb(descriptor), digest, prep["id"]),
            )
            conn.execute(
                "UPDATE analysis_checkpoints SET contract_digest=%s WHERE request_id=%s",
                (contract_digest(row["input_digest"], old), rid),
            )
            # Migration013 leaves historical identities NULL; do not count this
            # pre-recovery fixture as new-policy attempts.
            conn.execute(
                "UPDATE analysis_calls SET logical_digest=NULL WHERE request_id=%s", (rid,)
            )
    return old


@pytest.mark.parametrize("outcome", ["200", "400"])
def test_normal_resume_from_consumed_diagnostic_has_new_consent_and_runs_all_remaining_stages(
    db, tmp_path, outcome
):
    pid, cid, store, owner, worker, directory, rid, target, _, _, _, control = diagnostic_setup(
        db, tmp_path
    )
    try:
        prep = prepare_diagnostic(worker, pid, cid, rid)
        diagnostic = worker.admission.submit(pid, submission(prep))
        worker.start()
        diagnostic = terminal(worker, pid, str(diagnostic["id"]))
        did = str(diagnostic["id"])
        assert diagnostic["call_seq"] == 1 and diagnostic["error_code"].startswith(
            "provider_invalid_request"
        )
        current = copy.deepcopy(worker.profile)
        stop(worker, owner)
        old = historical_fixture_profiles(db, (rid, did), current)
        originals = {r: original(db, r) for r in (rid, did)}
        checkpoints = {r: read_rows(db, pid, r) for r in (rid, did)}
        ledgers = {
            r: db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (r,))
            for r in (rid, did)
        }
        quota = directory / "diagnostic-quota.json"
        initial_count = json.loads(quota.read_text())["count"]
        assert resume_block(None, originals[did], old) == "analysis_diagnostic_stopped"
        owner = AppOwnership(db)
        worker = AnalysisWorker(
            db,
            store,
            owner,
            profile=current,
            model_factory=diagnostic_factory,
            diagnostic_target=target,
        )
        worker.recover()
        worker.state.open()
        view = request_view(db, pid, did, current, target)
        assert view["resume_contract_changed"] and view["resume_block_reason"] is None
        assert not view["diagnostic_available"]
        assert prepare_diagnostic(worker, pid, cid, rid)["error_code"] == "analysis_resume_consumed"
        assert (
            prepare_diagnostic(worker, pid, cid, did)["error_code"]
            == "analysis_diagnostic_consumed"
        )
        prepared = prepare_resume(worker, pid, cid, did)
        assert prepared["state"] == "ready", prepared
        descriptor = prepared["descriptor"]
        assert not descriptor.get("diagnostic") and not descriptor.get("diagnostic_retry")
        assert descriptor["resume"]["checkpoint_count"] == len(checkpoints[did]) == 2
        assert descriptor["resume"]["compatibility"]["reused_stage"] == "discover_consolidate"
        document = worker.admission.transfer(pid, str(prepared["id"]), download=True)
        assert "diagnostic_request" not in document
        assert document["request_contract"] == current["request_contract"]
        assert prepared["consent_digest"] != originals[did]["consent"]["consent_digest"]
        body = submission(prepared)
        rejected = worker.admission.submit(
            pid,
            body.model_copy(update={"consent_digest": originals[did]["consent"]["consent_digest"]}),
        )
        assert rejected["state"] == "rejected"
        assert json.loads(quota.read_text())["count"] == initial_count
        control.write_text(outcome)
        accepted = worker.admission.submit(pid, submission(prepared))
        worker.start()
        done = terminal(worker, pid, str(accepted["id"]))
        assert done["inherited_attempts"] == originals[did]["inherited_attempts"] + 1
        fresh = db.all(
            "SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (done["id"],)
        )
        if outcome == "200":
            assert done["state"] == "completed", done
            assert [c["phase"] for c in fresh] == ["reconcile", "reconstruct", "audit", "assess"]
            assert len(Experiences(db).listing(pid)) == 1
        else:
            assert done["state"] == "failed" and done["error_code"].startswith(
                "provider_invalid_request"
            )
            assert [c["phase"] for c in fresh] == ["reconcile"]
            blocked = request_view(db, pid, str(done["id"]), current, target)
            assert not blocked["resume_contract_changed"]
            assert blocked["resume_block_reason"] == "analysis_resume_request_fix_required"
            assert (
                prepare_resume(worker, pid, cid, str(done["id"]))["error_code"]
                == "analysis_resume_request_fix_required"
            )
            assert not Experiences(db).listing(pid)
        assert json.loads(quota.read_text())["count"] == initial_count + len(fresh)
        copied = read_rows(db, pid, str(done["id"]))[:2]
        for a, b in zip(copied, checkpoints[did], strict=True):
            assert {k: v for k, v in a.items() if k not in {"request_id", "contract_digest"}} == {
                k: v for k, v in b.items() if k not in {"request_id", "contract_digest"}
            }
        for source in (rid, did):
            assert original(db, source) == originals[source]
            assert read_rows(db, pid, source) == checkpoints[source]
            assert (
                db.all("SELECT * FROM analysis_calls WHERE request_id=%s ORDER BY seq", (source,))
                == ledgers[source]
            )
        assert prepare_resume(worker, pid, cid, did)["error_code"] == "analysis_resume_consumed"
        assert not request_view(db, pid, rid, current, target)["diagnostic_available"]
    finally:
        stop(worker, owner)
