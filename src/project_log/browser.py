"""Read Collection observations without consulting the mutable repository."""

import base64
import stat
from typing import Literal

import psycopg

from project_log.db import Database, Row
from project_log.git import CollectionError
from project_log.policy import path_info

Category = Literal["files", "documents", "structure"]


class Browser:
    def __init__(self, db: Database):
        self.db = db

    def inventory(
        self, collection: str, *, connection: psycopg.Connection[Row] | None = None
    ) -> dict[bytes, list[Row]]:
        # Body references stay on their original observation; never resolve a later body.
        query = """SELECT id,layer,metadata,body_reason,observed_at,body_observed_at,
            sha256,provenance,repaired,original_body_reason,repair_read_metadata
            FROM working_records WHERE collection_id=%s
            UNION ALL SELECT id,'head',metadata,NULL,observed_at,NULL,NULL,
            '{"kind":"immutable_git_locator"}'::jsonb,false,NULL,NULL
            FROM head_records WHERE collection_id=%s"""
        rows = (
            connection.execute(query, (collection, collection)).fetchall()
            if connection is not None
            else self.db.all(query, (collection, collection))
        )
        paths: dict[bytes, list[Row]] = {}
        for row in rows:
            meta = row["metadata"]
            path = base64.b64decode(meta["path_b64"])
            mode = meta.get("mode")
            if path.endswith(b"/") or (isinstance(mode, int) and stat.S_ISDIR(mode)):
                continue
            # An ignored directory marker does not assert knowledge of its descendants.
            paths.setdefault(path, []).append(row)
        return paths

    def overview(self, collection: str) -> Row:
        with self.db.connect() as conn:
            # A deletion must not split the predecessor lookup and its observations.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            current = conn.execute(
                "SELECT * FROM collections WHERE id=%s", (collection,)
            ).fetchone()
            if current is None:
                raise CollectionError("not_found", "요청한 프로젝트 또는 수집 기록이 없습니다.")
            previous_row = conn.execute(
                """SELECT * FROM collections WHERE project_id=%s AND state='completed'
                AND (created_at,id)<(%s,%s) ORDER BY created_at DESC,id DESC LIMIT 1""",
                (current["project_id"], current["created_at"], current["id"]),
            ).fetchone()
            return self._overview_counts(collection, current, previous_row, conn)

    def _overview_counts(
        self, collection: str, current: Row, previous_row: Row | None, conn: psycopg.Connection[Row]
    ) -> Row:
        def counts(row: Row) -> Row:
            cid = str(row["id"])
            paths = self.inventory(cid, connection=conn)
            files = {
                path: variants
                for path, variants in paths.items()
                if not any(
                    v["metadata"].get("mode") == "160000" or v["body_reason"] == "submodule"
                    for v in variants
                )
            }
            docs = sum(p.startswith(b"docs/") for p in files)
            commit_count = conn.execute(
                "SELECT count(*) AS n FROM commits WHERE collection_id=%s", (cid,)
            ).fetchone()
            assert commit_count is not None
            observed = {
                "commits": commit_count["n"],
                "files": len(files) - docs,
                "documents": docs,
            }
            complete = row["state"] == "completed" and row["snapshot"].get("complete")
            return {
                "observed": observed,
                "counts": {
                    key: value
                    if complete and (key != "documents" or row["snapshot"].get("document_roots"))
                    else None
                    for key, value in observed.items()
                },
            }

        selected = counts(current)
        previous = counts(previous_row) if previous_row else None
        return {
            "collection_id": collection,
            # API compatibility: this is the live overview basis, not the stored capture baseline.
            "baseline_id": str(previous_row["id"]) if previous_row else None,
            "selected": selected,
            "previous": previous,
            "delta": {
                key: selected["counts"][key] - previous["counts"][key]
                if previous
                and selected["counts"][key] is not None
                and previous["counts"][key] is not None
                else None
                for key in ("commits", "files", "documents")
            },
        }

    def directory(
        self, collection: str, category: Category, directory_b64: str, limit: int, offset: int
    ) -> Row:
        self.db.detail(collection)
        try:
            directory = base64.b64decode(directory_b64, validate=True)
        except ValueError as exc:
            raise CollectionError("invalid_path", "조회 경로가 유효하지 않습니다.") from exc
        root = b"docs/" if category == "documents" else b""
        if not directory:
            directory = root
        if directory and not directory.endswith(b"/"):
            raise CollectionError("invalid_path", "디렉터리 경로가 필요합니다.")
        if category == "documents" and not directory.startswith(root):
            raise CollectionError("invalid_path", "개발 문서는 root docs/ 아래를 조회합니다.")
        children: dict[bytes, Row] = {}
        for path, variants in self.inventory(collection).items():
            is_document = path.startswith(b"docs/")
            if (
                category != "structure" and is_document != (category == "documents")
            ) or not path.startswith(directory):
                continue
            remaining = path[len(directory) :]
            name, separator, _ = remaining.partition(b"/")
            child = directory + name + (b"/" if separator else b"")
            children[child] = {
                **path_info(child),
                "name": name.decode("utf-8", "replace"),
                "directory": bool(separator),
                "observations": [] if separator else variants,
            }
        ordered = sorted(children.values(), key=lambda r: (not r["directory"], r["path_b64"]))
        return {
            "directory": path_info(directory),
            "total": len(ordered),
            "items": ordered[offset : offset + limit],
        }

    def commits(self, collection: str, limit: int, offset: int) -> Row:
        self.db.detail(collection)
        return {
            "total": self.db.one(
                "SELECT count(*) AS n FROM commits WHERE collection_id=%s", (collection,)
            )["n"],
            "items": self.db.all(
                """SELECT oid,metadata,body_reason,observed_at FROM commit_records
                WHERE collection_id=%s ORDER BY
                (metadata->'committer'->>'timestamp')::bigint DESC NULLS LAST,oid
                LIMIT %s OFFSET %s""",
                (collection, limit, offset),
            ),
        }

    def changes(self, collection: str, commit: str, limit: int, offset: int) -> Row:
        self.db.one(
            "SELECT oid FROM commits WHERE collection_id=%s AND oid=%s", (collection, commit)
        )
        return {
            "total": self.db.one(
                "SELECT count(*) AS n FROM changes WHERE collection_id=%s AND commit_oid=%s",
                (collection, commit),
            )["n"],
            "items": self.db.all(
                """SELECT id,commit_oid,parent_oid,metadata,body_reason,observed_at
                FROM change_records WHERE collection_id=%s AND commit_oid=%s
                ORDER BY parent_oid NULLS FIRST,id LIMIT %s OFFSET %s""",
                (collection, commit, limit, offset),
            ),
        }

    def documents(self, collection: str, limit: int, offset: int) -> Row:
        self.db.detail(collection)
        items = [
            {
                **path_info(path),
                "name": path.decode("utf-8", "replace")[5:],
                "directory": False,
                "observations": variants,
            }
            for path, variants in sorted(self.inventory(collection).items())
            if path.startswith(b"docs/")
        ]
        return {"total": len(items), "items": items[offset : offset + limit]}
