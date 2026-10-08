"""Collection lifecycle decisions and atomic, reference-aware DB removal."""

from typing import Annotated

import psycopg
from pydantic import BaseModel, ConfigDict, StringConstraints

from project_log.db import Database, Row
from project_log.git import CollectionError

ACTIVE = {"capturing", "queued", "running"}
CLEANUP = {"cancel_pending", "cleanup_pending", "partial", "failed"}


class Memo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str | None, StringConstraints(max_length=200)] = None
    description: Annotated[str | None, StringConstraints(max_length=4000)] = None


class Collections:
    def __init__(self, db: Database):
        self.db = db

    def _lock(self, conn: psycopg.Connection[Row], collection: str) -> Row:
        owner = conn.execute(
            "SELECT project_id FROM collections WHERE id=%s", (collection,)
        ).fetchone()
        if owner is None:
            raise CollectionError("not_found", "수집 기록이 없습니다.")
        conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (owner["project_id"],))
        row = conn.execute(
            "SELECT * FROM collections WHERE id=%s FOR UPDATE", (collection,)
        ).fetchone()
        if row is None:
            raise CollectionError("not_found", "수집 기록이 없습니다.")
        return row

    def cancel(self, collection: str) -> Row:
        with self.db.connect() as conn:
            row = self._lock(conn, collection)
            if row["state"] not in ACTIVE:
                raise CollectionError("conflict", "진행 중인 수집만 취소할 수 있습니다.")
            conn.execute("UPDATE collections SET state='cancel_pending' WHERE id=%s", (collection,))
        # This committed intent is deliberately separate from removal.
        return {"id": collection, "state": "cancel_pending"}

    def memo(self, collection: str, memo: Memo) -> Row:
        with self.db.connect() as conn:
            row = self._lock(conn, collection)
            if row["state"] != "completed":
                raise CollectionError("conflict", "완료된 수집만 제목과 설명을 편집할 수 있습니다.")
            updated = conn.execute(
                "UPDATE collections SET title=%s,description=%s WHERE id=%s RETURNING *",
                (
                    (memo.title or "").strip() or None,
                    (memo.description or "").strip() or None,
                    collection,
                ),
            ).fetchone()
            assert updated is not None
        return updated

    def remove(self, collection: str, *, cleanup: bool = False) -> None:
        # All observation deletion, reference detachment and orphan removal commit
        # together. No filesystem resource belongs to a Collection.
        with self.db.connect() as conn:
            try:
                row = self._lock(conn, collection)
            except CollectionError:
                if cleanup:
                    return
                raise
            if row["state"] not in (CLEANUP if cleanup else {"completed"}):
                raise CollectionError("conflict", "완료된 수집만 삭제할 수 있습니다.")
            project = row["project_id"]
            contents = conn.execute(
                """SELECT content_id AS id FROM working_entries WHERE collection_id=%s
                UNION SELECT r.content_id FROM working_repairs r JOIN working_entries w
                    ON w.id=r.working_entry_id WHERE w.collection_id=%s
                UNION SELECT content_id FROM commits WHERE collection_id=%s
                UNION SELECT content_id FROM changes WHERE collection_id=%s""",
                (collection,) * 4,
            ).fetchall()
            changes = conn.execute(
                "SELECT git_change_id AS id FROM changes WHERE collection_id=%s", (collection,)
            ).fetchall()
            files = conn.execute(
                "SELECT git_file_id AS id FROM head_files WHERE collection_id=%s", (collection,)
            ).fetchall()
            commits = conn.execute(
                "SELECT oid FROM commits WHERE collection_id=%s", (collection,)
            ).fetchall()
            conn.execute(
                """UPDATE collections SET baseline_id=NULL,
                summary=jsonb_set(summary,'{comparison}',
                    '{"available":false,"baseline_id":null,"reason":"baseline_deleted"}'::jsonb)
                WHERE baseline_id=%s""",
                (collection,),
            )
            conn.execute("UPDATE collections SET retry_of=NULL WHERE retry_of=%s", (collection,))
            conn.execute("DELETE FROM collection_repairs WHERE collection_id=%s", (collection,))
            conn.execute(
                """DELETE FROM working_repairs WHERE working_entry_id IN
                (SELECT id FROM working_entries WHERE collection_id=%s)""",
                (collection,),
            )
            for table in (
                "collection_issues",
                "changes",
                "head_files",
                "working_entries",
                "commits",
            ):
                conn.execute(f"DELETE FROM {table} WHERE collection_id=%s", (collection,))
            conn.execute("DELETE FROM collections WHERE id=%s", (collection,))
            conn.execute(
                """DELETE FROM git_changes g WHERE project_id=%s AND id=ANY(%s)
                AND NOT EXISTS (SELECT 1 FROM changes d WHERE d.git_change_id=g.id)""",
                (project, [r["id"] for r in changes]),
            )
            conn.execute(
                """DELETE FROM git_files g WHERE project_id=%s AND id=ANY(%s)
                AND NOT EXISTS (SELECT 1 FROM head_files h WHERE h.git_file_id=g.id)""",
                (project, [r["id"] for r in files]),
            )
            conn.execute(
                """DELETE FROM git_commits g WHERE project_id=%s AND oid=ANY(%s)
                AND NOT EXISTS (SELECT 1 FROM commits m WHERE m.project_id=g.project_id
                    AND m.oid=g.oid)
                AND NOT EXISTS (SELECT 1 FROM git_changes d WHERE d.project_id=g.project_id
                    AND d.commit_oid=g.oid)""",
                (project, [r["oid"] for r in commits]),
            )
            conn.execute(
                """DELETE FROM contents b WHERE project_id=%s AND id=ANY(%s)
                AND NOT EXISTS (SELECT 1 FROM working_entries w WHERE w.content_id=b.id)
                AND NOT EXISTS (SELECT 1 FROM working_repairs r WHERE r.content_id=b.id)
                AND NOT EXISTS (SELECT 1 FROM commits m WHERE m.content_id=b.id)
                AND NOT EXISTS (SELECT 1 FROM changes d WHERE d.content_id=b.id)""",
                (project, [r["id"] for r in contents if r["id"] is not None]),
            )
