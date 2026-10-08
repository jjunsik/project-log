import getpass
import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from project_log.git import CollectionError
from project_log.policy import content_reason, sha256


class CollectionStopped(Exception):
    """A committed lifecycle decision forbids further collection writes."""


Row = dict[str, Any]
RECORDS = {
    "commits": "commit_records",
    "changes": "change_records",
    "head_files": "head_records",
    "working_entries": "working_records",
}
POLICY_REASONS = {
    "sensitive_path",
    "suspected_secret",
    "ignored_metadata_only",
    "untracked_metadata_only",
    "binary",
    "non_utf8",
    "large",
    "large_diff",
    "symlink",
    "submodule",
    "non_regular",
}


def put_content(conn: psycopg.Connection[Row], project: str, raw: bytes | None) -> int | None:
    if raw is None:
        return None
    if content_reason(raw):
        raise CollectionError("unsafe_content", "안전 검사를 통과하지 않은 본문입니다.")
    digest = sha256(raw)
    row = conn.execute(
        """INSERT INTO contents(project_id,sha256,body) VALUES (%s,%s,%s)
        ON CONFLICT (project_id,sha256) DO NOTHING RETURNING id""",
        (project, digest, raw),
    ).fetchone()
    existing = conn.execute(
        "SELECT id,body FROM contents WHERE project_id=%s AND sha256=%s", (project, digest)
    ).fetchone()
    if existing is None or bytes(existing["body"]) != raw:
        raise CollectionError("content_identity_conflict", "본문 identity가 일치하지 않습니다.")
    return int(row["id"] if row else existing["id"])


def local_dsn(database: str = "project_log") -> str:
    return make_conninfo(dbname=database, host="127.0.0.1", port="5432", user=getpass.getuser())


def default_dsn() -> str:
    return os.environ.get("PROJECT_LOG_DATABASE_URL", local_dsn())


class Database:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or default_dsn()
        self.pending_cleanup: set[str] = set()
        self.pending_lock = threading.Lock()

    def connect(self) -> psycopg.Connection[Row]:
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)

    @contextmanager
    def writer(
        self, collection: str, *, completed: bool = False
    ) -> Iterator[psycopg.Connection[Row]]:
        # Cancellation UPDATE waits for existing writes. After it commits, every
        # subsequent write sees the durable state and aborts before touching data.
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state FROM collections WHERE id=%s FOR SHARE", (collection,)
            ).fetchone()
            if not row or row["state"] not in (
                {"capturing", "queued", "running"} | ({"completed"} if completed else set())
            ):
                raise CollectionStopped()
            yield conn

    def write_collection(self, collection: str, query: str, params: tuple[Any, ...]) -> None:
        with self.writer(collection) as conn:
            conn.execute(query, params)

    def mark_cleanup(self, collection: str) -> None:
        with self.pending_lock:
            self.pending_cleanup.add(collection)
        self.execute(
            "UPDATE collections SET state='cleanup_pending' WHERE id=%s "
            "AND state IN ('capturing','queued','running','partial','failed')",
            (collection,),
        )

        with self.pending_lock:
            self.pending_cleanup.discard(collection)

    def all(self, query: str, params: tuple[Any, ...] = ()) -> list[Row]:
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def one(self, query: str, params: tuple[Any, ...] = ()) -> Row:
        rows = self.all(query, params)
        if not rows:
            raise CollectionError("not_found", "요청한 프로젝트 또는 수집 기록이 없습니다.")
        return rows[0]

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as conn:
            conn.execute(query, params)

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(72304211)")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations
                (version text PRIMARY KEY, sha256 text NOT NULL,
                 applied_at timestamptz NOT NULL DEFAULT now())""")
            for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                content = file.read_bytes()
                checksum = hashlib.sha256(content).hexdigest()
                row = conn.execute(
                    "SELECT sha256 FROM schema_migrations WHERE version=%s", (file.name,)
                ).fetchone()
                if row:
                    if row["sha256"] != checksum:
                        raise RuntimeError("Applied migration checksum mismatch")
                    continue
                conn.execute(content.decode())
                conn.execute(
                    "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)",
                    (file.name, checksum),
                )

    def issue(
        self, collection: str, phase: str, code: str, message: str, context: Row | None = None
    ) -> None:
        self.write_collection(
            collection,
            """INSERT INTO collection_issues(collection_id,phase,code,message,context)
            VALUES (%s,%s,%s,%s,%s)""",
            (collection, phase, code, message, Jsonb(context or {})),
        )

    def detail(self, collection: str) -> Row:
        row = self.one("SELECT * FROM collections WHERE id=%s", (collection,))
        row["issues"] = self.all(
            "SELECT * FROM collection_issues WHERE collection_id=%s ORDER BY id", (collection,)
        )
        return row

    def summarize(self, collection: str) -> Row:
        with self.writer(collection, completed=True) as conn:
            counts: Row = {}
            for table in ("commits", "changes", "head_files", "working_entries"):
                counts[table] = self.one(
                    f"SELECT count(*) AS n FROM {table} WHERE collection_id=%s", (collection,)
                )["n"]
            counts["preserved_working_bodies"] = self.one(
                """SELECT count(*) AS n FROM working_records
                WHERE collection_id=%s AND body IS NOT NULL""",
                (collection,),
            )["n"]
            counts["preserved_diffs"] = self.one(
                """SELECT count(*) AS n FROM change_records
                WHERE collection_id=%s AND diff IS NOT NULL""",
                (collection,),
            )["n"]
            counts["document_candidates"] = self.one(
                """SELECT count(*) AS n FROM head_records
                WHERE collection_id=%s AND metadata->>'kind_hint'='document_candidate'""",
                (collection,),
            )["n"]
            reasons = self.all(
                """SELECT body_reason AS reason, count(*) AS count FROM (
                SELECT body_reason FROM working_records WHERE collection_id=%s
                UNION ALL SELECT body_reason FROM change_records WHERE collection_id=%s
                UNION ALL SELECT body_reason FROM commit_records WHERE collection_id=%s
                ) t WHERE body_reason IS NOT NULL GROUP BY body_reason ORDER BY body_reason""",
                (collection, collection, collection),
            )
            counts["body_exclusions"] = [r for r in reasons if r["reason"] in POLICY_REASONS]
            counts["body_errors"] = [
                r for r in reasons if r["reason"] not in POLICY_REASONS | {"missing", "deleted"}
            ]
            counts["errors"] = self.one(
                "SELECT count(*) AS n FROM collection_issues WHERE collection_id=%s", (collection,)
            )["n"]
            for layer in ("working", "untracked", "document"):
                label = "tracked_working" if layer == "working" else layer
                counts[f"preserved_{label}_bodies"] = self.one(
                    """SELECT count(*) AS n FROM working_records
                    WHERE collection_id=%s AND layer=%s AND body IS NOT NULL""",
                    (collection, layer),
                )["n"]
            counts["comparison"] = self.compare(collection)
            conn.execute(
                "UPDATE collections SET summary=%s WHERE id=%s", (Jsonb(counts), collection)
            )
            return counts

    def compare(self, collection: str) -> Row:
        current = self.one(
            "SELECT baseline_id,snapshot FROM collections WHERE id=%s", (collection,)
        )
        baseline = current["baseline_id"]
        result: Row = {"baseline_id": str(baseline) if baseline else None, "available": False}
        if not baseline:
            return {**result, "reason": "first_observation"}
        previous = self.one("SELECT snapshot FROM collections WHERE id=%s", (baseline,))
        if not current["snapshot"].get("complete") or not previous["snapshot"].get("complete"):
            return {**result, "reason": "incomplete_snapshot"}

        def inventory(cid: str) -> dict[tuple[str, str, int], str | None]:
            items = {}
            for row in self.all("SELECT * FROM working_records WHERE collection_id=%s", (cid,)):
                if row["layer"] == "ignored" or row["body_reason"] in {"missing", "deleted"}:
                    continue
                meta = row["metadata"]
                key = (row["layer"], meta["path_b64"], meta.get("stage", 0))
                items[key] = meta.get("oid") if row["layer"] == "index" else row["sha256"]
            return items

        before, after = inventory(str(baseline)), inventory(collection)
        shared = before.keys() & after.keys()
        known = {key for key in shared if before[key] is not None and after[key] is not None}
        return {
            **result,
            "available": True,
            "unit": "layer_path_stage_observation",
            "new": len(after.keys() - before.keys()),
            "deleted": len(before.keys() - after.keys()),
            "changed": sum(before[key] != after[key] for key in known),
            "unchanged": sum(before[key] == after[key] for key in known),
            "unknown": len(shared - known),
        }

    def observe_commit(
        self,
        collection: str,
        project: str,
        oid: str,
        metadata: Row,
        raw: bytes | None,
        reason: str | None,
    ) -> None:
        with self.writer(collection) as conn:
            conn.execute(
                """INSERT INTO git_commits(project_id,oid,metadata) VALUES (%s,%s,%s)
                ON CONFLICT (project_id,oid) DO NOTHING""",
                (project, oid, Jsonb(metadata)),
            )
            shared = conn.execute(
                "SELECT metadata FROM git_commits WHERE project_id=%s AND oid=%s", (project, oid)
            ).fetchone()
            assert shared is not None
            conn.execute(
                """INSERT INTO commits(collection_id,project_id,oid,metadata,content_id,body_reason)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    collection,
                    project,
                    oid,
                    None if shared["metadata"] == metadata else Jsonb(metadata),
                    put_content(conn, project, raw),
                    reason,
                ),
            )

    def observe_change(
        self,
        collection: str,
        project: str,
        oid: str,
        parent: str | None,
        metadata: Row,
        raw: bytes | None,
        reason: str | None,
    ) -> None:
        with self.writer(collection) as conn:
            key = (project, oid, parent, metadata["old"]["path_b64"], metadata["new"]["path_b64"])
            conn.execute(
                """INSERT INTO git_changes
                (project_id,commit_oid,parent_oid,old_path_b64,new_path_b64,metadata)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT
                (project_id,commit_oid,parent_oid,old_path_b64,new_path_b64) DO NOTHING""",
                (*key, Jsonb(metadata)),
            )
            shared = conn.execute(
                """SELECT id,metadata FROM git_changes WHERE project_id=%s AND commit_oid=%s
                AND parent_oid IS NOT DISTINCT FROM %s AND old_path_b64=%s AND new_path_b64=%s""",
                key,
            ).fetchone()
            assert shared is not None
            conn.execute(
                """INSERT INTO changes(collection_id,project_id,commit_oid,parent_oid,
                git_change_id,metadata,content_id,body_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    collection,
                    project,
                    oid,
                    parent,
                    shared["id"],
                    None if shared["metadata"] == metadata else Jsonb(metadata),
                    put_content(conn, project, raw),
                    reason,
                ),
            )

    def observe_head(self, collection: str, project: str, metadata: Row) -> None:
        with self.writer(collection) as conn:
            key = (project, metadata["commit"], metadata["path_b64"])
            conn.execute(
                """INSERT INTO git_files(project_id,commit_oid,path_b64,metadata)
                VALUES (%s,%s,%s,%s) ON CONFLICT (project_id,commit_oid,path_b64) DO NOTHING""",
                (*key, Jsonb(metadata)),
            )
            shared = conn.execute(
                """SELECT id,metadata FROM git_files
                WHERE project_id=%s AND commit_oid=%s AND path_b64=%s""",
                key,
            ).fetchone()
            assert shared is not None
            conn.execute(
                """INSERT INTO head_files(collection_id,project_id,git_file_id,metadata)
                VALUES (%s,%s,%s,%s)""",
                (
                    collection,
                    project,
                    shared["id"],
                    None if shared["metadata"] == metadata else Jsonb(metadata),
                ),
            )
