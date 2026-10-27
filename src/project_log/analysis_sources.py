"""Read the exact DB source version; metadata reads never resolve Git or convert files."""

import psycopg

from project_log.analysis_input import canonical
from project_log.db import Row
from project_log.git import CollectionError
from project_log.policy import sha256


def source_version(
    conn: psycopg.Connection[Row], project: str, end_collection: str, *, lock: bool = False
) -> Row:
    settings = conn.execute(
        "SELECT id,name,status,path,repository_key,base_branch FROM projects WHERE id=%s",
        (project,),
    ).fetchone()
    if settings is None:
        raise CollectionError("not_found", "해당 프로젝트가 없습니다.")
    end = conn.execute(
        "SELECT id,created_at FROM collections WHERE project_id=%s AND id=%s AND state='completed'",
        (project, end_collection),
    ).fetchone()
    if end is None:
        raise CollectionError("analysis_end_unavailable", "선택한 종료 수집을 사용할 수 없습니다.")
    if lock:
        # Collector completion takes a Collection lock, independently of Project.
        # Include unfinished predecessors so they cannot become completed mid-validation.
        conn.execute(
            "SELECT id FROM collections WHERE project_id=%s AND (created_at,id)<=(%s,%s) "
            "ORDER BY created_at,id FOR SHARE",
            (project, end["created_at"], end["id"]),
        ).fetchall()
    collections = conn.execute(
        "SELECT id,kind,retry_of,baseline_id,snapshot,policy,created_at,started_at,finished_at "
        "FROM collections WHERE project_id=%s AND state='completed' "
        "AND (created_at,id)<=(%s,%s) ORDER BY created_at,id",
        (project, end["created_at"], end["id"]),
    ).fetchall()
    ids = [c["id"] for c in collections]
    records: Row = {}
    for view, fields, order in (
        ("commit_records", "r.oid,r.metadata,r.body_reason,r.observed_at", "r.oid"),
        (
            "change_records",
            "r.id,r.commit_oid,r.parent_oid,r.metadata,r.body_reason,r.observed_at,"
            "r.content_id,b.sha256 AS content_sha256,"
            "CASE WHEN r.content_id IS NULL THEN encode(sha256(r.diff),'hex') END AS legacy_sha256",
            "r.id",
        ),
        (
            "working_records",
            "r.id,r.layer,r.metadata,r.sha256,r.body_reason,r.observed_at,r.body_observed_at,"
            "r.content_id,b.sha256 AS content_sha256,r.provenance,r.original_body_reason,"
            "r.repair_read_metadata,r.repaired,(r.body IS NOT NULL) AS body_available",
            "r.id",
        ),
        ("head_records", "r.id,r.metadata,r.observed_at,r.git_file_id", "r.id"),
    ):
        join = (
            "LEFT JOIN contents b ON b.id=r.content_id "
            if view in {"change_records", "working_records"}
            else ""
        )
        records[view] = conn.execute(
            f"SELECT r.collection_id,{fields} FROM {view} r {join} "
            f"WHERE r.project_id=%s AND r.collection_id=ANY(%s) ORDER BY r.collection_id,{order}",
            (project, ids),
        ).fetchall()
    materials = conn.execute(
        "SELECT id,filename,sha256,size_bytes,extension,media_type,added_at "
        "FROM user_materials WHERE project_id=%s ORDER BY added_at,id",
        (project,),
    ).fetchall()
    return {
        "fingerprint": sha256(
            canonical(
                {
                    "settings": settings,
                    "collections": collections,
                    "records": records,
                    "materials": materials,
                }
            ).encode()
        ),
        "collections": collections,
        "materials": materials,
    }
