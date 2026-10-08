import base64
import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from project_log.db import Database
from scripts.verification_db import temporary_database


def test_upgrade_001_preserves_records_bodies_times_and_is_idempotent():
    project, collection = str(uuid4()), str(uuid4())
    with temporary_database() as dsn:
        db = Database(dsn)
        initial = Path("src/project_log/migrations/001_initial.sql").read_bytes()
        with db.connect() as conn:
            conn.execute(initial.decode())
            conn.execute(
                """CREATE TABLE schema_migrations(version text PRIMARY KEY,sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            conn.execute(
                "INSERT INTO schema_migrations(version,sha256) VALUES ('001_initial.sql',%s)",
                (hashlib.sha256(initial).hexdigest(),),
            )
            conn.execute(
                """INSERT INTO projects
                (id,name,path,repository_key,status,coding_agent,repository_info)
                VALUES (%s,'old','/synthetic','/synthetic/.git','ongoing','none','{}')""",
                (project,),
            )
            conn.execute(
                """INSERT INTO collections(id,project_id,state,snapshot,policy,summary)
                VALUES (%s,%s,'completed',%s,'{}','{}')""",
                (collection, project, Jsonb({"complete": True})),
            )
            meta = {
                "path": "old.txt",
                "path_b64": base64.b64encode(b"old.txt").decode(),
                "status": " M",
            }
            conn.execute(
                """INSERT INTO working_entries
                (collection_id,layer,metadata,body,sha256,observed_at)
                VALUES (%s,'working',%s,%s,%s,now()),(%s,'index',%s,%s,%s,now())""",
                (
                    collection,
                    Jsonb(meta),
                    b"old raw\r\n",
                    hashlib.sha256(b"old raw\r\n").hexdigest(),
                    collection,
                    Jsonb(meta),
                    b"old raw\r\n",
                    hashlib.sha256(b"old raw\r\n").hexdigest(),
                ),
            )
            oid = "a" * 40
            conn.execute(
                "INSERT INTO commits(collection_id,oid,metadata,raw) VALUES (%s,%s,%s,%s)",
                (
                    collection,
                    oid,
                    Jsonb({"parents": [], "message": "old\n"}),
                    b"old commit\n",
                ),
            )
            change = {"old": meta, "new": meta}
            conn.execute(
                "INSERT INTO changes(collection_id,commit_oid,metadata,diff) VALUES (%s,%s,%s,%s)",
                (collection, oid, Jsonb(change), b"old diff\n"),
            )
            conn.execute(
                "INSERT INTO head_files(collection_id,metadata) VALUES (%s,%s)",
                (collection, Jsonb({**meta, "commit": oid, "oid": "b" * 40})),
            )
        before = {
            table: db.all(f"SELECT * FROM {table}")
            for table in ("collections", "working_entries", "commits", "changes", "head_files")
        }
        db.migrate()
        ledger = db.all("SELECT * FROM schema_migrations ORDER BY version")
        db.migrate()
        assert db.all("SELECT * FROM schema_migrations ORDER BY version") == ledger
        assert len(ledger) == 6
        assert db.one("SELECT base_branch FROM projects")["base_branch"] is None
        after = {
            "working_entries": "working_records",
            "commits": "commit_records",
            "changes": "change_records",
            "head_files": "head_records",
        }
        for original_table, view in after.items():
            old_rows, new_rows = before[original_table], db.all(f"SELECT * FROM {view}")
            assert len(old_rows) == len(new_rows)
            for old, new in zip(old_rows, new_rows, strict=True):
                assert {k: new[k] for k in old} == old
        original_collection = before["collections"][0]
        migrated = db.one("SELECT * FROM collections")
        assert {k: migrated[k] for k in original_collection} == original_collection
        assert db.one("SELECT count(*) AS n FROM contents")["n"] == 3
        assert not db.one("SELECT count(*) AS n FROM working_entries WHERE body IS NOT NULL")["n"]
        assert (
            db.one("SELECT count(*) AS n FROM pg_constraint WHERE conname='one_active_collection'")[
                "n"
            ]
            == 0
        )
        index = db.one("SELECT indexdef FROM pg_indexes WHERE indexname='one_active_collection'")
        assert all(state in index["indexdef"] for state in ("capturing", "queued", "running"))
        assert (
            db.one(
                "SELECT count(*) AS n FROM information_schema.views WHERE table_schema='public'"
            )["n"]
            == 4
        )
        with db.connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO contents(project_id,sha256,body) VALUES (%s,'wrong',%s)",
                (project, b"raw"),
            )
