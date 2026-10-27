"""Isolated PostgreSQL only; no original Seed content or product DB mutation."""

import pytest
from psycopg.types.json import Jsonb

from project_log.experiences import Seed


@pytest.mark.parametrize("change", ["missing_policy", "instruction_digest"])
def test_spawned_new_source_rejects_old_parent_contract_before_provider_entry(db, tmp_path, change):
    from test_analysis_worker import accepted, setup_worker, stop, terminal

    pid, cid, _, owner, worker, directory = setup_worker(db, tmp_path)
    try:
        if change == "missing_policy":
            worker.profile.pop("consolidation_policy")
        else:
            worker.profile["instruction_digest"] = "0" * 64
        worker.admission.profile = worker.profile
        request, _ = accepted(worker, pid, cid)
        worker.start()
        failed = terminal(worker, pid, request)
        assert failed["state"] == "failed"
        assert failed["error_code"] == "analysis_policy_changed"
        assert failed["call_seq"] == 0
        assert not db.all("SELECT * FROM analysis_checkpoints WHERE request_id=%s", (request,))
        assert not list(directory.glob("attempt-*"))
    finally:
        stop(worker, owner)


@pytest.mark.parametrize("refs,terms", [(1, 0), (7, 3)])
def test_read_only_jsonb_size_aggregation_matches_compact_pydantic_json(db, refs, terms):
    seed = Seed(
        title='합성 Seed "quotes" \\ escaped',
        rationale="한글과 ☃\n\t\r\b\f\u0001 spaces , : / \u2028",
        evidence_ids=[f"ref-{i}" for i in range(refs)],
        search_terms=[f"term-{i}" for i in range(terms)],
    )
    stats = db.one(
        "SELECT length(s::text)-7-greatest(jsonb_array_length(s->'evidence_ids')-1,0)"
        "-greatest(jsonb_array_length(s->'search_terms')-1,0) AS chars,"
        "octet_length(s::text)-7-greatest(jsonb_array_length(s->'evidence_ids')-1,0)"
        "-greatest(jsonb_array_length(s->'search_terms')-1,0) AS bytes "
        "FROM (SELECT %s::jsonb AS s) q",
        (Jsonb(seed.model_dump()),),
    )
    assert stats == {
        "chars": len(seed.model_dump_json()),
        "bytes": len(seed.model_dump_json().encode()),
    }
