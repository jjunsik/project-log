import getpass
import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from project_log.db import Database, default_dsn, local_dsn
from scripts.verification_db import temporary_database


def test_local_tcp_default_and_explicit_url_override(monkeypatch):
    monkeypatch.delenv("PROJECT_LOG_DATABASE_URL", raising=False)
    assert conninfo_to_dict(Database().dsn) == {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "project_log",
        "user": getpass.getuser(),
    }
    explicit = "postgresql://deployment-user@database.example.invalid:6543/deployment_db"
    monkeypatch.setenv("PROJECT_LOG_DATABASE_URL", explicit)
    assert default_dsn() == explicit
    assert Database().dsn == explicit


def test_verification_database_isolated_and_dropped_after_failure(monkeypatch):
    monkeypatch.setenv("PROJECT_LOG_DATABASE_URL", "postgresql://invalid.invalid/product")
    with pytest.raises(RuntimeError, match="synthetic verification failure"):
        with temporary_database() as dsn:
            info = conninfo_to_dict(dsn)
            assert info["host"] == "127.0.0.1" and info["port"] == "5432"
            assert info["dbname"].startswith("project_log_verify_")
            assert info["dbname"] != "project_log"
            database = Database(dsn)
            database.migrate()
            database.migrate()
            files = sorted(
                (Path(__file__).resolve().parents[1] / "src/project_log/migrations").glob("*.sql")
            )
            assert database.all(
                "SELECT version,sha256 FROM schema_migrations ORDER BY version"
            ) == [
                {"version": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in files
            ]
            raise RuntimeError("synthetic verification failure")
    with psycopg.connect(local_dsn("postgres")) as conn:
        assert not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (info["dbname"],)
        ).fetchone()


def test_relation_identity_migration_preserves_legacy_values_and_null_identity():
    migration = (
        Path(__file__).resolve().parents[1]
        / "src/project_log/migrations/013_relation_recovery_identity.sql"
    )
    with temporary_database() as dsn:
        database = Database(dsn)
        with database.connect() as conn:
            # Only the pre-existing Ledger shape is needed to exercise this additive DDL.
            conn.execute(
                "CREATE TABLE analysis_calls (request_id uuid,project_id uuid,"
                "generation uuid,owner_token uuid,seq integer,phase text,payload_digest text,"
                "entered_at timestamptz DEFAULT now(),outcome text,"
                "finished_at timestamptz,error_code text)"
            )
            conn.execute(
                "INSERT INTO analysis_calls(request_id,project_id,seq,phase,"
                "payload_digest,outcome,error_code) "
                "VALUES (%s,%s,1,'reconcile',%s,'responded',%s)",
                (uuid4(), uuid4(), "a" * 64, "candidate_relation_grounding_invalid"),
            )
            before = conn.execute("SELECT to_jsonb(t) AS value FROM analysis_calls t").fetchone()[
                "value"
            ]
            conn.execute(migration.read_text())
            after = conn.execute("SELECT to_jsonb(t) AS value FROM analysis_calls t").fetchone()[
                "value"
            ]
            assert after.pop("logical_digest") is None and after == before
            assert conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname='analysis_relation_logical_identity'"
            ).fetchone()
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute("UPDATE analysis_calls SET logical_digest='not-a-digest'")
