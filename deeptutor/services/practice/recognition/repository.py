"""SQLite adapter hidden behind the recognition module's interface."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from deeptutor.services.practice.scheduler import DAY

_RUNNING = ("queued", "validating", "parsing", "recognizing")
_RECOVERABLE = (*_RUNNING, "ready")


class RecognitionJobRepository:
    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = db_path
        self.clock = clock

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, row: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT id FROM practice_recognition_jobs "
                "WHERE workspace_key=? AND status IN ('queued','validating','parsing','recognizing') "
                "LIMIT 1",
                (row["workspace_key"],),
            ).fetchone()
            if active is not None:
                raise ValueError("A question recognition job is already running")
            conn.execute(
                """
                INSERT INTO practice_recognition_jobs(
                    id, workspace_key, filename, mime_type, source_hash,
                    target, course_id, status, stage, completed_units,
                    total_units, progress_message, created_at, updated_at, expires_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', '', 0, 1, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["workspace_key"],
                    row["filename"],
                    row["mime_type"],
                    row["source_hash"],
                    row["target"],
                    row["course_id"],
                    "Waiting to recognize questions",
                    row["created_at"],
                    row["created_at"],
                    row["expires_at"],
                ),
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        result = dict(row)
        for key, default in (
            ("source_attachment_json", {}),
            ("drafts_json", []),
            ("summary_json", {}),
        ):
            try:
                result[key.removesuffix("_json")] = json.loads(result.get(key) or "")
            except (TypeError, json.JSONDecodeError):
                result[key.removesuffix("_json")] = default
            result.pop(key, None)
        return result

    def get(self, job_id: str, workspace_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM practice_recognition_jobs WHERE id=? AND workspace_key=?",
                (job_id, workspace_key),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= self.clock() and row["status"] in {
                "ready",
                "staged",
                "failed",
            }:
                conn.execute(
                    "UPDATE practice_recognition_jobs SET status='expired', updated_at=?, "
                    "version=version+1 WHERE id=?",
                    (self.clock(), job_id),
                )
                row = conn.execute(
                    "SELECT * FROM practice_recognition_jobs WHERE id=?", (job_id,)
                ).fetchone()
        return self._decode(row)

    def latest_recoverable(self, workspace_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM practice_recognition_jobs
                WHERE workspace_key=? AND status IN (?, ?, ?, ?, ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (workspace_key, *_RECOVERABLE),
            ).fetchone()
        if row is None:
            return None
        job = self.get(str(row["id"]), workspace_key)
        if job is None or job["status"] not in _RECOVERABLE:
            return None
        return job

    def set_source_attachment(
        self,
        job_id: str,
        workspace_key: str,
        attachment: dict[str, str],
    ) -> None:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET source_attachment_json=?, updated_at=?
                WHERE id=? AND workspace_key=? AND status='queued'
                """,
                (
                    json.dumps(attachment, ensure_ascii=False),
                    self.clock(),
                    job_id,
                    workspace_key,
                ),
            ).rowcount
        if not changed:
            raise LookupError("Recognition job cannot accept its source attachment")

    def progress(
        self,
        job_id: str,
        workspace_key: str,
        *,
        status: str,
        stage: str,
        message: str,
        completed_units: int | None = None,
        total_units: int | None = None,
    ) -> None:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET status=?, stage=?, progress_message=?,
                    completed_units=COALESCE(?, completed_units),
                    total_units=COALESCE(?, total_units),
                    updated_at=?, version=version+1
                WHERE id=? AND workspace_key=? AND status IN ('queued','validating','parsing','recognizing')
                """,
                (
                    status,
                    stage,
                    message,
                    completed_units,
                    total_units,
                    self.clock(),
                    job_id,
                    workspace_key,
                ),
            ).rowcount
        if not changed:
            raise LookupError("Recognition job is not running")

    def ready(self, job_id: str, workspace_key: str, drafts: list[dict]) -> None:
        summary = {
            "total": len(drafts),
            "normal": sum(d.get("review_level") == "normal" for d in drafts),
            "review": sum(d.get("review_level") == "review" for d in drafts),
            "required": sum(d.get("review_level") == "required" for d in drafts),
        }
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET status='ready', stage='recognizing', completed_units=total_units,
                    progress_message=?, drafts_json=?, summary_json=?, updated_at=?,
                    version=version+1
                WHERE id=? AND workspace_key=? AND status='recognizing'
                """,
                (
                    f"Recognized {len(drafts)} questions",
                    json.dumps(drafts, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    self.clock(),
                    job_id,
                    workspace_key,
                ),
            ).rowcount
        if not changed:
            raise LookupError("Recognition job cannot become ready")

    def fail(self, job_id: str, workspace_key: str, code: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET status='failed', error_code=?, error_message=?, progress_message=?,
                    updated_at=?, version=version+1
                WHERE id=? AND workspace_key=? AND status IN ('queued','validating','parsing','recognizing')
                """,
                (code, message[:1000], message[:200], self.clock(), job_id, workspace_key),
            )

    def staged(
        self,
        job_id: str,
        workspace_key: str,
        expected_version: int,
        token: str,
    ) -> None:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET status='staged', import_token=?, updated_at=?, version=version+1
                WHERE id=? AND workspace_key=? AND status='ready' AND version=?
                """,
                (token, self.clock(), job_id, workspace_key, expected_version),
            ).rowcount
        if not changed:
            raise ValueError("Recognition job changed; reload it before importing")

    def reconcile_running(self, workspace_key: str | None = None) -> int:
        where = "status IN ('queued','validating','parsing','recognizing')"
        params: list[object] = []
        if workspace_key is not None:
            where += " AND workspace_key=?"
            params.append(workspace_key)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE practice_recognition_jobs
                SET status='failed', error_code='worker_lost',
                    error_message='Recognition worker stopped before completing the job',
                    progress_message='Recognition interrupted', updated_at=?, version=version+1
                WHERE {where}
                """,  # nosec B608 - fixed predicate plus bound scope value
                [self.clock(), *params],
            )
        return int(cursor.rowcount)

    def purge_expired(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM practice_recognition_jobs WHERE expires_at < ?",
                (self.clock() - DAY,),
            )
        return int(cursor.rowcount)


__all__ = ["RecognitionJobRepository"]
