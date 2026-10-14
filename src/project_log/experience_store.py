"""Atomic, project-scoped storage of completed candidates and the exact local evidence."""

import base64
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from project_log.db import Database, Row
from project_log.experience_input import ExperienceInput
from project_log.experiences import AnalysisResult
from project_log.git import CollectionError
from project_log.policy import sha256


class Experiences:
    def __init__(self, db: Database):
        self.db = db

    def save(
        self,
        snapshot: ExperienceInput,
        result: AnalysisResult,
        *,
        model: str,
        mode: str = "isolated",
    ) -> Row:
        if result.input_digest != snapshot.digest or mode != "isolated":
            raise CollectionError(
                "analysis_input_mismatch",
                "입력 버전이 다르거나 실제 전송 저장 경로가 비활성입니다.",
            )
        run = str(uuid4())
        with self.db.connect() as conn:
            if not conn.execute(
                "SELECT id FROM projects WHERE id=%s FOR UPDATE", (snapshot.project_id,)
            ).fetchone():
                raise CollectionError("not_found", "해당 프로젝트가 없습니다.")
            conn.execute(
                """INSERT INTO experience_runs
                (id,project_id,input_digest,input_manifest,result,model,mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run,
                    snapshot.project_id,
                    snapshot.digest,
                    Jsonb(snapshot.manifest()),
                    Jsonb(result.model_dump()),
                    model,
                    mode,
                ),
            )
            for e in snapshot.entries:
                conn.execute(
                    """INSERT INTO experience_evidence
                    (run_id,project_id,reference,source,body,media,reason,limitations)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        run,
                        snapshot.project_id,
                        e.reference,
                        Jsonb(json.loads(e.source_json)),
                        e.text,
                        Jsonb(
                            [
                                {
                                    **m.descriptor(),
                                    "base64": base64.b64encode(m.raw).decode("ascii"),
                                }
                                for m in e.media
                            ]
                        ),
                        e.reason,
                        Jsonb(list(e.limitations)),
                    ),
                )
        return self.detail(snapshot.project_id, run)

    def listing(self, project: str) -> list[Row]:
        self.db.one("SELECT id FROM projects WHERE id=%s", (project,))
        return self.db.all(
            """SELECT id,created_at,input_digest,model,mode,
            jsonb_array_length(result->'candidates') AS candidate_count,
            result->'coverage' AS coverage FROM experience_runs WHERE project_id=%s
            ORDER BY created_at DESC,id DESC""",
            (project,),
        )

    def detail(self, project: str, run: str) -> Row:
        return self.db.one(
            "SELECT * FROM experience_runs WHERE project_id=%s AND id=%s", (project, run)
        )

    def evidence(self, project: str, run: str, reference: str) -> Row:
        row = self.db.one(
            "SELECT * FROM experience_evidence WHERE project_id=%s AND run_id=%s AND reference=%s",
            (project, run, reference),
        )
        row["media"] = [{k: v for k, v in m.items() if k != "base64"} for m in row["media"]]
        return row

    def media(self, project: str, run: str, reference: str, index: int) -> tuple[Row, bytes]:
        row = self.db.one(
            "SELECT media FROM experience_evidence WHERE project_id=%s "
            "AND run_id=%s AND reference=%s",
            (project, run, reference),
        )
        if index < 0 or index >= len(row["media"]):
            raise CollectionError("not_found", "비텍스트 근거가 없습니다.")
        item = row["media"][index]
        raw = base64.b64decode(item["base64"], validate=True)
        if sha256(raw) != item["sha256"]:
            raise CollectionError("storage_corrupt", "보존 근거의 무결성 검사에 실패했습니다.")
        return {k: v for k, v in item.items() if k != "base64"}, raw
