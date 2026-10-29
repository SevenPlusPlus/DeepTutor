"""Transactional review history and import staging in the user's notebook DB."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .scheduler import DAY, Rating, day_bounds, schedule
from .mastery import (
    apply_mastery,
    initialize_mastery,
    mastery_evidence,
    record_exposure,
    recently_exposed,
    target_for_entry,
)

# Legacy correct or manually mastered questions start with a three-day interval.
# Once reviewed, the saved adaptive schedule is authoritative.
_DUE_AT_SQL = """(CASE WHEN (n.resolved=1 OR n.is_correct=1)
    AND COALESCE(r.review_count, 0)=0 THEN n.updated_at + 259200
    ELSE COALESCE(r.due_at, n.created_at) END)"""


_ACTIVE_SQL = (
    "r.is_mistake=1 AND r.learning_status IN ('open','verify') "
    "AND r.paused=0 AND n.resolved=0 "
    "AND NOT EXISTS(SELECT 1 FROM practice_variants av WHERE av.entry_id=n.id "
    "AND av.validation_status!='approved')"
)


class ReviewConflict(ValueError):
    """A different tab or source changed this question since it was opened."""


def reconcile_photo_links(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """Reconcile the archive's cache without recreating deleted notebook entries.

    Call inside a write transaction so a confirmation cannot race a notebook
    deletion between validating links and deciding whether it is idempotent.
    """
    links = conn.execute(
        "SELECT l.draft_id,l.entry_id FROM photo_question_links l "
        "JOIN notebook_entries n ON n.id=l.entry_id WHERE l.item_id=? ORDER BY l.draft_id",
        (row["id"],),
    ).fetchall()
    live = {link["draft_id"]: link["entry_id"] for link in links}
    entry_ids = list(dict.fromkeys(live.values()))
    drafts = json.loads(row["drafts_json"])
    missing = any(
        draft["draft_id"] not in live for draft in drafts if draft.get("selected", True)
    )
    status = row["status"]
    if status == "confirmed" and missing:
        status = (
            "needs_subject" if any(draft.get("subject") == "unclassified" for draft in drafts)
            else "ready"
        )
    if entry_ids != json.loads(row["entry_ids_json"]) or status != row["status"]:
        conn.execute(
            "DELETE FROM photo_question_links WHERE item_id=? "
            "AND NOT EXISTS(SELECT 1 FROM notebook_entries n WHERE n.id=photo_question_links.entry_id)",
            (row["id"],),
        )
        conn.execute(
            "UPDATE photo_inbox_items SET entry_ids_json=?,status=?,version=version+1 WHERE id=?",
            (json.dumps(entry_ids), status, row["id"]),
        )
        row = conn.execute("SELECT * FROM photo_inbox_items WHERE id=?", (row["id"],)).fetchone()
    return row


def initialize_practice(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='practice_review_state'"
    ).fetchone()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS practice_review_state (
            entry_id INTEGER PRIMARY KEY REFERENCES notebook_entries(id) ON DELETE CASCADE,
            is_mistake INTEGER NOT NULL DEFAULT 1,
            first_wrong_at REAL NOT NULL,
            due_at REAL NOT NULL,
            interval_days REAL NOT NULL DEFAULT 1,
            ease REAL NOT NULL DEFAULT 2.5,
            streak INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            last_review_at REAL,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_practice_due ON practice_review_state(due_at, entry_id);
        CREATE TABLE IF NOT EXISTS practice_review_events (
            request_id TEXT PRIMARY KEY,
            entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            rating TEXT NOT NULL,
            answer TEXT NOT NULL,
            reviewed_at REAL NOT NULL,
            outcome_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_practice_history ON practice_review_events(entry_id, reviewed_at);
        CREATE TABLE IF NOT EXISTS practice_imports (
            token TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            target TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            result_json TEXT
        );
        CREATE TABLE IF NOT EXISTS practice_recognition_jobs (
            id TEXT PRIMARY KEY,
            workspace_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            source_attachment_json TEXT NOT NULL DEFAULT '{}',
            target TEXT NOT NULL,
            course_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            completed_units INTEGER NOT NULL DEFAULT 0,
            total_units INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            parser_signature TEXT NOT NULL DEFAULT '',
            model_ref TEXT NOT NULL DEFAULT '',
            drafts_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1,
            import_token TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_practice_recognition_scope_created
        ON practice_recognition_jobs(workspace_key, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS practice_capture_insert
        AFTER INSERT ON notebook_entries
        WHEN NEW.is_correct = 0 AND COALESCE(NEW.result, '') != 'ungraded'
        BEGIN
            INSERT INTO practice_review_state(entry_id, first_wrong_at, due_at)
            SELECT NEW.id, NEW.created_at, NEW.created_at + 86400
            WHERE NOT EXISTS(SELECT 1 FROM practice_review_state WHERE entry_id=NEW.id);
        END;
        CREATE TRIGGER IF NOT EXISTS practice_invalidate_question
        AFTER UPDATE OF question, options_json, correct_answer ON notebook_entries
        WHEN OLD.question != NEW.question OR OLD.options_json != NEW.options_json OR OLD.correct_answer != NEW.correct_answer
        BEGIN
            UPDATE practice_review_state SET version=version+1 WHERE entry_id=NEW.id;
        END;
        DROP TRIGGER IF EXISTS practice_capture_update;
        CREATE TRIGGER practice_capture_update
        AFTER UPDATE OF is_correct, result ON notebook_entries
        WHEN NEW.is_correct = 0 AND COALESCE(NEW.result, '') != 'ungraded'
        BEGIN
            INSERT INTO practice_review_state(entry_id, first_wrong_at, due_at)
            SELECT NEW.id, NEW.updated_at, NEW.updated_at + 86400
            WHERE NOT EXISTS(SELECT 1 FROM practice_review_state WHERE entry_id=NEW.id);
            UPDATE practice_review_state SET
                first_wrong_at = CASE WHEN is_mistake=0 THEN NEW.updated_at ELSE first_wrong_at END,
                is_mistake = 1,
                due_at = NEW.updated_at + 86400, interval_days = 1, streak = 0,
                version = version + 1
            WHERE entry_id = NEW.id AND (OLD.is_correct = 1 OR OLD.result = 'ungraded');
        END;
    """)
    if not existing:
        # Additive migration: original IDs, answers, timestamps and categories stay intact.
        conn.execute("""
            INSERT OR IGNORE INTO practice_review_state(entry_id, first_wrong_at, due_at)
            SELECT id, created_at, created_at + 86400 FROM notebook_entries
            WHERE is_correct = 0 AND COALESCE(result, '') != 'ungraded'
        """)

    initialize_mastery(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS practice_variants (
            entry_id INTEGER PRIMARY KEY REFERENCES notebook_entries(id) ON DELETE CASCADE,
            parent_entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            target_skill TEXT NOT NULL,
            introduces_new_skill INTEGER NOT NULL DEFAULT 0,
            validation_status TEXT NOT NULL DEFAULT 'approved',
            created_at REAL NOT NULL
        );
        DROP TRIGGER IF EXISTS practice_invalidate_question;
        CREATE TRIGGER practice_invalidate_question
        AFTER UPDATE OF question, options_json, correct_answer ON notebook_entries
        WHEN OLD.question != NEW.question OR OLD.options_json != NEW.options_json OR OLD.correct_answer != NEW.correct_answer
        BEGIN
            INSERT INTO practice_mastery_decisions(entry_id,action,decided_at,reason,evidence_json,policy_version)
            SELECT NEW.id,'reopened',NEW.updated_at,'question_changed','{}','mistake-mastery-v1'
            FROM practice_review_state WHERE entry_id=NEW.id AND learning_status='closed';
            UPDATE practice_review_state SET version=version+1,learning_status='open',
                closed_at=NULL,evidence_since=NEW.updated_at WHERE entry_id=NEW.id;
            UPDATE practice_variants SET validation_status='stale'
                WHERE parent_entry_id=NEW.id OR entry_id=NEW.id;
        END;
        DROP TRIGGER IF EXISTS practice_reopen_source_failure;
        CREATE TRIGGER practice_reopen_source_failure
        AFTER UPDATE OF is_correct, result, attempt_count ON notebook_entries
        WHEN NEW.is_correct=0 AND COALESCE(NEW.result,'')!='ungraded'
            AND (OLD.is_correct=1 OR OLD.result='ungraded' OR NEW.attempt_count>OLD.attempt_count)
        BEGIN
            INSERT INTO practice_mastery_decisions(entry_id,action,decided_at,reason,evidence_json,policy_version)
            SELECT NEW.id,'reopened',NEW.updated_at,'source_failure','{}','mistake-mastery-v1'
            FROM practice_review_state WHERE entry_id=NEW.id AND learning_status='closed';
            UPDATE practice_review_state SET learning_status='open',closed_at=NULL,
                evidence_since=NEW.updated_at,due_at=NEW.updated_at+86400,interval_days=1,streak=0,
                first_wrong_at=CASE WHEN is_mistake=0 THEN NEW.updated_at ELSE first_wrong_at END,
                is_mistake=1,lapses=lapses+1,
                version=version+CASE WHEN OLD.is_correct=1 OR OLD.result='ungraded' THEN 0 ELSE 1 END
                WHERE entry_id=NEW.id;
            UPDATE notebook_entries SET resolved=0 WHERE id=NEW.id;
        END;
    """)


class PracticeStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _scope(session_ids: list[str] | None, course_id: str = "") -> tuple[str, list]:
        # Approved variations inherit their parent's course/session scope, while
        # independent imports remain visible without a synthetic conversation.
        sql = """(n.session_id IS NULL OR s.deleted_at IS NULL)
            AND NOT EXISTS(SELECT 1 FROM practice_variants hidden
                JOIN notebook_entries parent ON parent.id=hidden.parent_entry_id
                JOIN sessions ps ON ps.id=parent.session_id
                WHERE hidden.entry_id=n.id AND ps.deleted_at IS NOT NULL)"""
        params: list = []
        if course_id:
            placeholders = ",".join("?" for _ in (session_ids or ())) or "NULL"
            sql += f""" AND (n.course_id=? OR n.session_id IN ({placeholders})
                OR EXISTS(SELECT 1 FROM practice_variants v
                    JOIN notebook_entries parent ON parent.id=v.parent_entry_id
                    LEFT JOIN sessions ps ON ps.id=parent.session_id
                    WHERE v.entry_id=n.id AND v.validation_status='approved'
                    AND (parent.session_id IS NULL OR ps.deleted_at IS NULL)
                    AND (parent.course_id=? OR parent.session_id IN ({placeholders}))))"""
            params.extend(
                [course_id, *(session_ids or ()), course_id, *(session_ids or ())]
            )
        elif session_ids is not None:
            placeholders = ",".join("?" for _ in session_ids) or "NULL"
            sql += f""" AND (n.session_id IN ({placeholders}) OR EXISTS(
                SELECT 1 FROM practice_variants v JOIN notebook_entries parent ON parent.id=v.parent_entry_id
                LEFT JOIN sessions ps ON ps.id=parent.session_id
                WHERE v.entry_id=n.id AND v.validation_status='approved'
                AND (parent.session_id IS NULL OR ps.deleted_at IS NULL)
                AND parent.session_id IN ({placeholders})))"""
            params.extend([*session_ids, *session_ids])
        return sql, params

    def overview(
        self,
        timezone: str,
        session_ids: list[str] | None = None,
        now: float | None = None,
        course_id: str = "",
    ) -> dict:
        now = time.time() if now is None else now
        start, end = day_bounds(timezone, now)
        scope, params = self._scope(session_ids, course_id)
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                    COALESCE(SUM(r.is_mistake=1),0) AS mistakes,
                    COALESCE(SUM(CASE WHEN {_ACTIVE_SQL} AND {_DUE_AT_SQL}<=? THEN 1 ELSE 0 END),0) AS due,
                    COALESCE(SUM(CASE WHEN {_ACTIVE_SQL} AND {_DUE_AT_SQL}<? THEN 1 ELSE 0 END),0) AS overdue,
                    MIN(CASE WHEN {_ACTIVE_SQL} THEN {_DUE_AT_SQL} END) AS next_due_at,
                    COALESCE(SUM(CASE WHEN {_ACTIVE_SQL} AND r.learning_status='open' THEN 1 ELSE 0 END),0) AS open,
                    COALESCE(SUM(CASE WHEN {_ACTIVE_SQL} AND r.learning_status='verify' THEN 1 ELSE 0 END),0) AS verify,
                    COALESCE(SUM(r.is_mistake=1 AND r.learning_status='closed'),0) AS closed,
                    COALESCE(SUM(r.is_mistake=1 AND (r.paused=1 OR (n.resolved=1 AND r.learning_status!='closed'))),0) AS paused
                    FROM notebook_entries n LEFT JOIN sessions s ON s.id=n.session_id
                    LEFT JOIN practice_review_state r ON r.entry_id=n.id WHERE {scope}""",  # nosec B608
                [now, start, *params],
            ).fetchone()
            completed = conn.execute(
                f"""SELECT COUNT(DISTINCT e.entry_id) FROM practice_review_events e
                JOIN notebook_entries n ON n.id=e.entry_id LEFT JOIN sessions s ON s.id=n.session_id
                WHERE e.reviewed_at>=? AND e.reviewed_at<? AND {scope}""",  # nosec B608
                [start, end, *params],
            ).fetchone()[0]
        return {
            **dict(row),
            "reviewed_today": completed,
            "day_end": end,
            "timezone": timezone,
        }

    def analytics(
        self,
        timezone: str,
        days: int = 30,
        session_ids: list[str] | None = None,
        now: float | None = None,
        course_id: str = "",
    ) -> dict:
        from .analytics import analytics

        scope, params = self._scope(session_ids, course_id)
        with self.connect() as conn:
            return analytics(
                conn, timezone, days, time.time() if now is None else now, scope, params
            )

    def queue(
        self,
        timezone: str,
        session_ids: list[str] | None = None,
        category_id: int | None = None,
        limit: int = 20,
        now: float | None = None,
        course_id: str = "",
    ) -> list[int]:
        now = time.time() if now is None else now
        day_bounds(timezone, now)
        scope, params = self._scope(session_ids, course_id)
        if category_id is not None:
            scope += " AND EXISTS(SELECT 1 FROM notebook_entry_categories c WHERE c.entry_id=n.id AND c.category_id=?)"
            params.append(category_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT n.id FROM notebook_entries n LEFT JOIN sessions s ON s.id=n.session_id
                LEFT JOIN practice_review_state r ON r.entry_id=n.id
                WHERE {_DUE_AT_SQL} <= ? AND {_ACTIVE_SQL} AND {scope}
                ORDER BY {_DUE_AT_SQL}, n.id LIMIT ?
            """,  # nosec B608 - fixed scope SQL; values bound
                [now, *params, limit],
            ).fetchall()  # nosec B608
        return [row["id"] for row in rows]

    @staticmethod
    def _state(conn: sqlite3.Connection, entry_id: int) -> dict:
        row = conn.execute(
            "SELECT r.*,n.resolved AS legacy_resolved FROM practice_review_state r JOIN notebook_entries n ON n.id=r.entry_id WHERE r.entry_id=?",
            (entry_id,),
        ).fetchone()
        result = (
            dict(row)
            if row
            else {
                "entry_id": entry_id,
                "is_mistake": 0,
                "version": 0,
                "review_count": 0,
                "interval_days": 1,
                "due_at": None,
                "learning_status": "open",
                "paused": False,
                "closed_at": None,
            }
        )
        legacy_resolved = result.pop("legacy_resolved", False)
        result["paused"] = bool(
            result["paused"] or (legacy_resolved and result["learning_status"] != "closed")
        )
        result["evidence"] = mastery_evidence(conn, entry_id)
        return result

    def state(self, entry_id: int) -> dict:
        with self.connect() as conn:
            return self._state(conn, entry_id)

    def recommendations(
        self,
        timezone: str,
        session_ids: list[str] | None = None,
        course_id: str = "",
        category_id: int | None = None,
        budget_minutes: int = 15,
        subject: str = "",
        query: str = "",
        include_early: bool = False,
        limit: int = 20,
        now: float | None = None,
        grade: str = "",
    ) -> list[dict]:
        from .recommendation import rank_recommendations

        now = time.time() if now is None else now
        day_bounds(timezone, now)
        if not 1 <= budget_minutes <= 120 or not 1 <= limit <= 100:
            raise ValueError("Invalid review budget or limit")
        scope, params = self._scope(session_ids, course_id)
        if category_id is not None:
            scope += " AND EXISTS(SELECT 1 FROM notebook_entry_categories c WHERE c.entry_id=n.id AND c.category_id=?)"
            params.append(category_id)
        if not include_early:
            scope += f" AND {_DUE_AT_SQL}<=?"
            params.append(now)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT n.id,n.question,n.question_type,n.difficulty,n.knowledge_point_id,n.section_title,
                    n.recognition_meta_json,n.created_at,r.learning_status,r.review_count,r.interval_days,
                    r.lapses,r.last_review_at,{_DUE_AT_SQL} AS due_at,
                    (SELECT GROUP_CONCAT(c.name,' ') FROM notebook_categories c
                        JOIN notebook_entry_categories ec ON ec.category_id=c.id WHERE ec.entry_id=n.id) AS tags
                    FROM notebook_entries n LEFT JOIN sessions s ON s.id=n.session_id
                    JOIN practice_review_state r ON r.entry_id=n.id
                    WHERE {_ACTIVE_SQL} AND {scope} ORDER BY {_DUE_AT_SQL},n.id""",  # nosec B608
                params,
            ).fetchall()
        return rank_recommendations(
            [dict(row) for row in rows],
            now=now,
            budget_minutes=budget_minutes,
            limit=limit,
            subject=subject,
            query=query,
            grade=grade,
        )

    def expose(
        self, entry_id: int, request_id: str, kind: str, now: float | None = None
    ) -> dict:
        if kind not in {"hint", "answer"}:
            raise ValueError("Unknown exposure kind")
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._visible_entry(conn, entry_id) is None:
                raise LookupError("Question not found")
            try:
                return record_exposure(conn, entry_id, request_id, kind, now)
            except ValueError as exc:
                raise ReviewConflict(str(exc)) from exc

    def update_learning(self, entry_id: int, version: int, paused: bool) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entry = self._visible_entry(conn, entry_id)
            if entry is None:
                raise LookupError("Question not found")
            state = conn.execute(
                "SELECT * FROM practice_review_state WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if state is None:
                raise ValueError("Only enrolled mistakes can be paused")
            if state["version"] != version:
                raise ReviewConflict(
                    "This question changed in another tab. Reload it before continuing."
                )
            conn.execute(
                "UPDATE practice_review_state SET paused=?,version=version+1 WHERE entry_id=?",
                (int(paused), entry_id),
            )
            if not paused:
                conn.execute(
                    "UPDATE notebook_entries SET resolved=0 WHERE id=?", (entry_id,)
                )
        return self.state(entry_id)

    def mastery_details(self, entry_id: int) -> dict:
        if not self.is_visible(entry_id):
            raise LookupError("Question not found")
        with self.connect() as conn:
            history = []
            for row in conn.execute(
                "SELECT a.*,e.answer,e.rating FROM practice_mastery_attempts a JOIN practice_review_events e ON e.request_id=a.request_id WHERE a.target_entry_id=? ORDER BY a.reviewed_at DESC,a.rowid DESC",
                (entry_id,),
            ):
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                item["correct"] = None if item["correct"] is None else bool(item["correct"])
                item["independent"] = bool(item["independent"])
                history.append(item)
            decisions = []
            for row in conn.execute(
                "SELECT * FROM practice_mastery_decisions WHERE entry_id=? ORDER BY id DESC",
                (entry_id,),
            ):
                item = dict(row)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                decisions.append(item)
        state = self.state(entry_id)
        return {
            "state": state,
            "evidence": state["evidence"],
            "history": history,
            "decisions": decisions,
        }

    @staticmethod
    def _visible_entry(conn: sqlite3.Connection, entry_id: int):
        scope, params = PracticeStore._scope(None)
        return conn.execute(
            f"SELECT n.* FROM notebook_entries n LEFT JOIN sessions s ON s.id=n.session_id WHERE n.id=? AND {scope}",  # nosec B608
            [entry_id, *params],
        ).fetchone()

    def is_visible(self, entry_id: int) -> bool:
        with self.connect() as conn:
            return self._visible_entry(conn, entry_id) is not None

    def review(
        self,
        entry_id: int,
        request_id: str,
        version: int,
        rating: Rating,
        answer: str,
        now: float | None = None,
        *,
        self_report: bool = False,
        hint_used: bool = False,
        answer_revealed: bool = False,
        attempt_kind: str = "original",
        timezone: str = "UTC",
        auto_grade: bool = False,
    ) -> dict:
        now = time.time() if now is None else now
        day_bounds(timezone, now)
        if attempt_kind not in {"original", "spotcheck"}:
            raise ValueError("Unknown review attempt kind")
        requested_rating = rating
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM practice_review_events WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                saved = json.loads(previous["outcome_json"])
                fingerprint = {
                    "self_report": self_report,
                    "hint_used": hint_used,
                    "answer_revealed": answer_revealed,
                    "attempt_kind": attempt_kind,
                    "timezone": timezone,
                    "auto_grade": auto_grade,
                }
                defaults = {
                    "self_report": False,
                    "hint_used": False,
                    "answer_revealed": False,
                    "attempt_kind": "original",
                    "timezone": "UTC",
                    "auto_grade": False,
                }
                if (
                    previous["entry_id"] != entry_id
                    or previous["answer"] != answer
                    or saved.get("requested_rating", previous["rating"]) != requested_rating
                    or any(
                        saved.get(key, defaults[key]) != value
                        for key, value in fingerprint.items()
                    )
                ):
                    raise ReviewConflict(
                        "This submission ID was already used for a different answer"
                    )
                return saved
            entry = self._visible_entry(conn, entry_id)
            if entry is None:
                raise LookupError("Question not found")
            state = self._state(conn, entry_id)
            if state["version"] != version:
                raise ReviewConflict(
                    "This question changed in another tab. Reload it before reviewing again."
                )
            lineage_parent_id, variant_kind, same_skill = target_for_entry(conn, entry_id)
            # An extension that introduces a new skill becomes its own learning
            # target, so a later mistake can eventually be mastered in its own right.
            target_id = lineage_parent_id if same_skill else entry_id
            if lineage_parent_id != entry_id:
                parent = conn.execute(
                    "SELECT n.id FROM notebook_entries n LEFT JOIN sessions s ON s.id=n.session_id WHERE n.id=? AND (n.session_id IS NULL OR s.deleted_at IS NULL)",
                    (lineage_parent_id,),
                ).fetchone()
                if parent is None:
                    raise LookupError("Parent question not found")
            from .answers import check_answer

            correct = None if self_report else check_answer(dict(entry), answer)
            if correct is False:
                if auto_grade:
                    rating = "again"
                elif rating != "again":
                    raise ValueError("An incorrect answer must be reviewed again")
            exposed = recently_exposed(
                conn, entry_id, target_id if same_skill else entry_id, now
            )
            independent = (
                correct is True
                and rating != "again"
                and not hint_used
                and not answer_revealed
                and not exposed
            )
            schedule_rating = (
                "hard"
                if correct is True and not independent and rating != "again"
                else rating
            )
            next_state = schedule(state, schedule_rating, now)
            mistake = bool(state.get("is_mistake")) or (
                rating == "again" and (target_id == entry_id or not same_skill)
            )
            conn.execute(
                """INSERT INTO practice_review_state(entry_id,is_mistake,first_wrong_at,due_at,
                    interval_days,ease,streak,lapses,review_count,last_review_at,version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(entry_id) DO UPDATE SET is_mistake=excluded.is_mistake,due_at=excluded.due_at,
                    first_wrong_at=CASE WHEN practice_review_state.is_mistake=0 AND excluded.is_mistake=1 THEN excluded.first_wrong_at ELSE practice_review_state.first_wrong_at END,
                    interval_days=excluded.interval_days,ease=excluded.ease,streak=excluded.streak,
                    lapses=excluded.lapses,review_count=excluded.review_count,
                    last_review_at=excluded.last_review_at,version=excluded.version""",
                (
                    entry_id,
                    int(mistake),
                    now,
                    *(
                        next_state[key]
                        for key in (
                            "due_at",
                            "interval_days",
                            "ease",
                            "streak",
                            "lapses",
                            "review_count",
                            "last_review_at",
                            "version",
                        )
                    ),
                ),
            )
            if rating == "again":
                conn.execute(
                    "UPDATE notebook_entries SET resolved=0 WHERE id=?", (entry_id,)
                )
            if target_id != entry_id and same_skill:
                conn.execute(
                    "INSERT OR IGNORE INTO practice_review_state(entry_id,is_mistake,first_wrong_at,due_at) VALUES(?,0,?,?)",
                    (target_id, now, now + DAY),
                )
            metadata = {
                "self_report": self_report,
                "hint_used": hint_used,
                "answer_revealed": answer_revealed,
                "recent_exposure": exposed,
                "grading_source": "objective" if correct is not None else "self_report",
                "introduces_new_skill": not same_skill,
                "variant_kind": variant_kind,
                "timezone": timezone,
            }
            outcome = {
                **next_state,
                "entry_id": entry_id,
                "is_mistake": mistake,
                "correct": correct,
                "rating": rating,
                "requested_rating": requested_rating,
                "self_report": self_report,
                "hint_used": hint_used,
                "answer_revealed": answer_revealed,
                "attempt_kind": attempt_kind,
                "timezone": timezone,
                "auto_grade": auto_grade,
                "independent": independent,
                "mastered": rating in {"good", "easy"},
            }
            conn.execute(
                "INSERT INTO practice_review_events VALUES(?,?,?,?,?,?)",
                (request_id, entry_id, rating, answer, now, json.dumps(outcome)),
            )
            conn.execute(
                "INSERT INTO practice_mastery_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    entry_id,
                    target_id,
                    (variant_kind if same_skill else None) or attempt_kind,
                    None if correct is None else int(correct),
                    int(independent),
                    int(same_skill or target_id == entry_id),
                    now,
                    json.dumps(metadata),
                ),
            )
            if target_id == entry_id:
                decision = apply_mastery(
                    conn, entry_id, correct=correct, independent=independent, now=now
                )
            else:
                decision = apply_mastery(
                    conn, target_id, correct=correct, independent=independent, now=now
                )
                conn.execute(
                    "UPDATE practice_review_state SET version=version+1,last_review_at=?,"
                    "lapses=lapses+?,streak=CASE WHEN ? THEN 0 ELSE streak END,"
                    "interval_days=CASE WHEN ? THEN 1 ELSE interval_days END WHERE entry_id=?",
                    (
                        now,
                        int(correct is False),
                        correct is False,
                        correct is False,
                        target_id,
                    ),
                )
                conn.execute(
                    "UPDATE practice_review_state SET learning_status=? WHERE entry_id=?",
                    ("open" if correct is False else "verify", entry_id),
                )
            # Persist feedback exposure after deciding this first submission. A reopened
            # tab may not use the answer it has just seen as fresh mastery evidence.
            record_exposure(conn, entry_id, "feedback-" + request_id, "feedback", now)
            current_state = self._state(conn, entry_id)
            outcome.update(current_state)
            outcome.update({key: decision[key] for key in ("closed", "reopened")})
            if lineage_parent_id != entry_id:
                outcome.update(
                    parent_entry_id=lineage_parent_id,
                    parent_state=self._state(conn, lineage_parent_id),
                )
            conn.execute(
                "UPDATE practice_review_events SET outcome_json=? WHERE request_id=?",
                (json.dumps(outcome), request_id),
            )
        return outcome

    def stage_import(
        self,
        filename: str,
        target: str,
        questions: list[dict],
        course_id: str = "",
        *,
        photo_context: dict | None = None,
    ) -> str:
        token, now = uuid.uuid4().hex, time.time()
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM practice_imports WHERE created_at < ? AND result_json IS NULL",
                (now - DAY,),
            )
            conn.execute(
                "INSERT INTO practice_imports VALUES(?, ?, ?, ?, ?, NULL)",
                (
                    token,
                    filename[:200],
                    target,
                    json.dumps(
                        {
                            "questions": questions,
                            "course_id": course_id,
                            **({"photo_context": photo_context} if photo_context else {}),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return token

    def commit_import(self, token: str) -> dict:
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            staged = conn.execute(
                "SELECT * FROM practice_imports WHERE token=?", (token,)
            ).fetchone()
            if not staged:
                raise LookupError("Import preview not found. Select the file again.")
            if staged["result_json"]:
                return json.loads(staged["result_json"])
            if now - staged["created_at"] > DAY:
                raise ValueError("Import preview expired. Select the file again.")
            payload = json.loads(staged["payload_json"])
            questions = payload if isinstance(payload, list) else payload["questions"]
            course_id = "" if isinstance(payload, list) else str(payload.get("course_id") or "")
            photo = None if isinstance(payload, list) else payload.get("photo_context")
            if photo:
                archived = conn.execute(
                    "SELECT * FROM photo_inbox_items WHERE id=? AND workspace_key=?",
                    (photo["item_id"], photo["workspace_key"]),
                ).fetchone()
                if archived is None:
                    raise LookupError("Photo not found")
                archived = reconcile_photo_links(conn, archived)
                if archived["archived_at"] is not None:
                    raise ReviewConflict("Restore the photo before confirming")
                if archived["status"] == "confirmed":
                    result = {"created": 0, "duplicates": len(questions), "total": len(questions)}
                    conn.execute(
                        "UPDATE practice_imports SET result_json=?,payload_json='[]' WHERE token=?",
                        (json.dumps(result), token),
                    )
                    return result
                if archived["version"] != photo["expected_version"]:
                    raise ReviewConflict("Photo changed; reload it before continuing")
            # All practice-file imports share one durable origin namespace.
            # ``question_id`` is a content hash, so re-importing the same
            # question remains idempotent without tying ownership to an
            # ephemeral preview receipt or a synthetic chat session.
            origin_ref = "partner-photo" if photo else "practice-import"
            created, skipped = 0, 0
            entry_ids = []
            for question in questions:
                if photo:
                    retained = conn.execute(
                        "SELECT l.entry_id FROM photo_question_links l JOIN notebook_entries n "
                        "ON n.id=l.entry_id WHERE l.item_id=? AND l.draft_id=?",
                        (photo["item_id"], question["recognition_meta"]["draft_id"]),
                    ).fetchone()
                    if retained is not None:
                        # Partial re-confirmation restores only deleted entries;
                        # the remaining question and its review plan stay intact.
                        entry_ids.append(retained["entry_id"])
                        skipped += 1
                        continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO notebook_entries(
                        session_id, origin_type, origin_ref, question_id, question, question_type,
                        options_json, correct_answer, explanation, difficulty, user_answer, source,
                        result, assessment_type, created_at, updated_at, material_title, material_id,
                        course_id, question_images_json, source_locator_json, answer_origin,
                        recognition_meta_json)
                    VALUES(NULL, 'external_import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quiz', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        origin_ref,
                        question["question_id"],
                        question["question"],
                        question["question_type"],
                        json.dumps(question["options"], ensure_ascii=False),
                        question["correct_answer"],
                        question["explanation"],
                        question["difficulty"],
                        question["user_answer"],
                        "partner_chat" if photo else "import",
                        "incorrect" if staged["target"] == "mistakes" and not photo else "ungraded",
                        now,
                        now,
                        staged["filename"],
                        "import:" + token,
                        course_id,
                        json.dumps(question.get("question_images", []), ensure_ascii=False),
                        json.dumps(question.get("source_locator", {}), ensure_ascii=False),
                        str(question.get("answer_origin") or ""),
                        json.dumps(question.get("recognition_meta", {}), ensure_ascii=False),
                    ),
                )
                created += cursor.rowcount
                skipped += 1 - cursor.rowcount
                entry_id = conn.execute(
                    """SELECT id FROM notebook_entries
                    WHERE origin_type='external_import' AND origin_ref=?
                      AND question_id=? AND turn_id=''""",
                    (origin_ref, question["question_id"]),
                ).fetchone()[0]
                entry_ids.append(entry_id)
                if cursor.rowcount and question.get("knowledge_point_id"):
                    conn.execute(
                        "UPDATE notebook_entries SET knowledge_point_id=? WHERE id=?",
                        (str(question["knowledge_point_id"]), entry_id),
                    )
                if question.get("correct_answer"):
                    record_exposure(conn, entry_id, f"import-{token}-{entry_id}", "answer", now)
                if photo:
                    conn.execute(
                        "INSERT OR IGNORE INTO photo_question_links(item_id,draft_id,entry_id) VALUES(?,?,?)",
                        (photo["item_id"], question["recognition_meta"]["draft_id"], entry_id),
                    )
                if staged["target"] == "mistakes":
                    conn.execute(
                        "INSERT OR IGNORE INTO practice_review_state(entry_id, first_wrong_at, due_at) VALUES(?, ?, ?)",
                        (entry_id, now, now + DAY),
                    )
                    if not photo:
                        conn.execute(
                            """UPDATE practice_review_state SET is_mistake=1, first_wrong_at=?,
                                due_at=?, interval_days=1, version=version+1
                                WHERE entry_id=? AND is_mistake=0""",
                            (now, now + DAY, entry_id),
                        )
                for tag in question["tags"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO notebook_categories(name, created_at) VALUES(?, ?)",
                        (tag, now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO notebook_entry_categories(entry_id, category_id) SELECT ?, id FROM notebook_categories WHERE name=?",
                        (entry_id, tag),
                    )
            result = {"created": created, "duplicates": skipped, "total": created + skipped}
            if photo:
                entry_ids = [
                    row["entry_id"] for row in conn.execute(
                        "SELECT l.entry_id FROM photo_question_links l JOIN notebook_entries n "
                        "ON n.id=l.entry_id WHERE l.item_id=? ORDER BY l.draft_id", (photo["item_id"],),
                    )
                ]
                # Keep the archive card consistent when the caller edits subjects
                # directly during confirmation, using the same all-draft policy
                # as the archive update operation (including unselected drafts).
                subjects = {draft["subject"] for draft in photo["drafts"]}
                subject = next(iter(subjects)) if len(subjects) == 1 else "unclassified"
                conn.execute(
                    "UPDATE photo_inbox_items SET status='confirmed',drafts_json=?,entry_ids_json=?,"
                    "subject=?,import_token=?,version=version+1 WHERE id=?",
                    (
                        json.dumps(photo["drafts"], ensure_ascii=False),
                        json.dumps(list(dict.fromkeys(entry_ids))),
                        subject,
                        token,
                        photo["item_id"],
                    ),
                )
            conn.execute(
                "UPDATE practice_imports SET result_json=?, payload_json='[]' WHERE token=?",
                (json.dumps(result), token),
            )
            conn.execute(
                """
                UPDATE practice_recognition_jobs
                SET status='committed', updated_at=?, version=version+1
                WHERE import_token=? AND status='staged'
                """,
                (now, token),
            )
        return result
