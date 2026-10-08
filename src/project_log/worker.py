import threading

import psycopg

from project_log.collections import Collections
from project_log.collector import collect
from project_log.db import CollectionStopped, Database


class Worker:
    """One local worker per database; unfinished attempts are cleaned, never resumed."""

    def __init__(self, db: Database):
        self.db = db
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="project-log-collector", daemon=True)
        self.lock = db.connect()
        self.lock.autocommit = True
        row = self.lock.execute("SELECT pg_try_advisory_lock(72304212) AS locked").fetchone()
        if not row or not row["locked"]:
            self.lock.close()
            raise RuntimeError("Another Project Log process is already using this database")
        self.error: str | None = None
        self.pending_failure: str | None = None

    def start(self) -> None:
        # Ownership lock means no worker from the previous process remains valid.
        # Preserve an already committed cancel intent, including after cleanup failure.
        self.db.execute("""UPDATE collections SET state='cleanup_pending'
            WHERE state IN ('capturing','queued','running')""")
        # Multiple legacy partial/failed attempts can coexist in a Project.
        # They are already cleanup targets; do not promote them into the unique active slot.
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join()
        self.lock.close()

    def cleanup(self) -> bool:
        rows = self.db.all(
            "SELECT id FROM collections WHERE state "
            "IN ('cancel_pending','cleanup_pending','partial','failed') ORDER BY created_at"
        )
        cleaned = False
        for row in rows:
            try:
                Collections(self.db).remove(str(row["id"]), cleanup=True)
                cleaned = True
            except Exception:
                self.error = "수집 자료 정리가 대기 중입니다. DB와 앱 상태를 확인하세요."
        return cleaned

    def once(self) -> bool:
        with self.db.pending_lock:
            pending = list(self.db.pending_cleanup)
        for collection in pending:
            self.db.mark_cleanup(collection)
        if self.pending_failure:
            self.db.mark_cleanup(self.pending_failure)
            self.pending_failure = None
        cleaned = self.cleanup()
        with self.db.connect() as conn:
            row = conn.execute("""UPDATE collections SET state='running',started_at=now()
                WHERE id=(SELECT id FROM collections WHERE state='queued'
                          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING id""").fetchone()
        if not row:
            return cleaned
        collection = str(row["id"])
        self.pending_failure = collection
        try:
            collect(self.db, collection, self.stop_event.is_set)
        except CollectionStopped:
            # A cancelled/deleted row must never be revived or turned into failure.
            pass
        except Exception:
            # Generic diagnostics only: exception strings can contain source/secret data.
            self.db.mark_cleanup(collection)
        # If DB marking failed, pending_failure remains for a later loop iteration.
        if self.stop_event.is_set():
            self.db.mark_cleanup(collection)
        self.pending_failure = None
        self.cleanup()
        return True

    def loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.lock.execute("SELECT 1")
                self.error = None
                self.once()
            except psycopg.Error:
                self.error = "DB 연결 문제로 수집 또는 정리가 대기 중입니다. DB 상태를 확인하세요."
            except Exception:
                self.error = "수집 정리가 대기 중입니다. 앱 상태를 확인하세요."
            self.stop_event.wait(0.3)
