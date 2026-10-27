"""Project-local mutation guard. Call while holding the existing Project row lock."""

import psycopg

from project_log.db import Row
from project_log.git import CollectionError


def require_mutable(conn: psycopg.Connection[Row], project: str) -> None:
    if conn.execute(
        "SELECT id FROM analysis_requests WHERE project_id=%s "
        "AND state IN ('queued','running','cancel_requested') LIMIT 1",
        (project,),
    ).fetchone():
        raise CollectionError(
            "analysis_active", "AI 분석이 끝난 뒤 자료·설정을 변경하세요. 분석 취소는 가능합니다."
        )
