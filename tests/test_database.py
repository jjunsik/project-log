import getpass

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
            assert database.one("SELECT count(*) AS n FROM schema_migrations")["n"] == 6
            raise RuntimeError("synthetic verification failure")
    with psycopg.connect(local_dsn("postgres")) as conn:
        assert not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (info["dbname"],)
        ).fetchone()
