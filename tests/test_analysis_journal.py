"""Journal reads and safe DB diagnostics; temporary DB, no model dispatch."""

import json
import logging
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_analysis_worker import accepted, setup_worker, stop

from project_log.api import create_app


@pytest.mark.parametrize(
    "state,legacy,unknown",
    [("queued", False, False), ("failed", False, False), ("failed", True, True)],
)
def test_empty_journal_distinguishes_new_execution_from_legacy_history(
    db, tmp_path, state, legacy, unknown
):
    pid, cid, _, owner, worker, _ = setup_worker(db, tmp_path)
    try:
        request, _ = accepted(worker, pid, cid)
        if state == "failed":
            db.execute(
                "UPDATE analysis_requests SET state='failed',error_code='synthetic',"
                "finished_at=now() WHERE id=%s",
                (request,),
            )
        if legacy:
            db.execute(
                "UPDATE analysis_requests SET consent=jsonb_set(consent,'{descriptor,profile}',"
                "(consent->'descriptor'->'profile')-'durability_policy') WHERE id=%s",
                (request,),
            )
        with TestClient(create_app(db, start_worker=False)) as client:
            base = f"/api/projects/{pid}/analysis/requests/{request}"
            for suffix in ("/events", "/partial"):
                response = client.get(base + suffix)
                assert response.status_code == 200
                data = response.json()
                assert data["total"] == 0
                assert data["execution_state"] == state
                assert data["historical_detail_unknown"] is unknown
                assert response.headers["cache-control"] == "no-store"
        assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == 0
    finally:
        stop(worker, owner)


@pytest.mark.parametrize(
    "surface,expected_stage",
    [
        ("events", "events_page"),
        ("event", "event_detail"),
        ("partial", "partial_candidate_validation"),
        ("evidence", "partial_evidence_request_lookup"),
        ("unknown", "partial_candidate_validation"),
    ],
)
def test_journal_db_failure_logs_only_safe_stage_sqlstate_and_correlation_and_can_be_requeried(
    db, tmp_path, monkeypatch, caplog, surface, expected_stage
):
    pid, cid, _, owner, worker, _ = setup_worker(db, tmp_path)
    try:
        request, _ = accepted(worker, pid, cid)
        error = (
            psycopg.OperationalError("SYNTHETIC_PRIVATE_SQL_BODY_PASSWORD")
            if surface == "unknown"
            else psycopg.errors.DeadlockDetected("SYNTHETIC_PRIVATE_SQL_BODY_PASSWORD")
        )
        with TestClient(create_app(db, start_worker=False)) as client:
            original_all, original_one = db.all, db.one

            def all_rows(query, params=()):
                if "FROM analysis_events" in query:
                    raise error
                return original_all(query, params)

            def one_row(query, params=()):
                if "SELECT input_payload" in query:
                    raise error
                return original_one(query, params)

            def reject(*args, **kwargs):
                raise error

            suffix = {
                "events": "/events",
                "event": "/events/1",
                "partial": "/partial",
                "unknown": "/partial",
                "evidence": "/partial/evidence",
            }[surface]
            url = f"/api/projects/{pid}/analysis/requests/{request}" + suffix
            with (
                monkeypatch.context() as patch,
                caplog.at_level(logging.ERROR, logger="project_log.api"),
            ):
                if surface in {"events", "event"}:
                    patch.setattr(db, "all", all_rows)
                elif surface == "evidence":
                    patch.setattr(db, "one", one_row)
                else:
                    patch.setattr("project_log.analysis_api.validated_candidates", reject)
                response = client.get(url + "?reference=SYNTHETIC_QUERY_SECRET")
            assert response.status_code == 503
            body = response.json()
            assert body["code"] == "database_unavailable"
            UUID(body["diagnostic_id"])
            assert body["diagnostic_id"] in body["detail"]
            records = [r for r in caplog.records if r.name == "project_log.api"]
            assert len(records) == 1
            diagnostic = json.loads(records[0].getMessage())
            assert diagnostic["diagnostic_id"] == body["diagnostic_id"]
            assert diagnostic["stage"] == expected_stage
            assert diagnostic["sqlstate"] == ("UNKNOWN" if surface == "unknown" else "40P01")
            assert diagnostic["cause_status"] == "UNKNOWN"
            assert diagnostic["elapsed_ms"] >= 0
            assert "SYNTHETIC_PRIVATE" not in caplog.text + response.text
            assert "SYNTHETIC_QUERY_SECRET" not in caplog.text + response.text
            assert not records[0].exc_info
            assert (
                client.get(f"/api/projects/{pid}/analysis/requests/{request}/partial").status_code
                == 200
            )
        assert db.one("SELECT count(*) AS n FROM analysis_calls")["n"] == 0
    finally:
        stop(worker, owner)
