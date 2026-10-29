"""Keep original photos before attempting recognition or importing questions.

The caller supplies an authenticated user/workspace context. Both the database
and attachment store are resolved inside that context; channel credentials never
belong in this module. A lease makes processing recoverable after cancellation.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from pathlib import Path
import time
import uuid

from PIL import Image, UnidentifiedImageError

from deeptutor.services.practice.normalization import cell_text, normalize_question
from deeptutor.services.practice.recognition import StartRecognitionRequest
from deeptutor.services.practice.recognition.service import (
    MAX_DRAFTS,
    MAX_SOURCE_BYTES,
    QuestionRecognitionService,
    VisionQuestionExtractor,
)
from deeptutor.services.practice.storage import PracticeStore, ReviewConflict, reconcile_photo_links
from deeptutor.services.storage import AttachmentStore, get_attachment_store

SUBJECTS = {
    "chinese": "语文",
    "math": "数学",
    "english": "英语",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
    "history": "历史",
    "geography": "地理",
    "politics": "道法",
    "other": "其他",
    "unclassified": "未分类",
}
PENDING = ("queued", "recognizing", "needs_subject", "needs_retake", "non_question", "failed")
_PUBLIC_SOURCE_FIELDS = ("partner_id", "channel", "partner_name")
_LEASE_SECONDS = 240
_RECOGNITION_TIMEOUT = 180


def _subject(value: object) -> str:
    result = str(value or "unclassified").strip()
    if result not in SUBJECTS:
        raise ValueError("Unknown subject")
    return result


class PhotoIngestionService:
    def __init__(
        self,
        db_path: Path,
        extractor: VisionQuestionExtractor,
        *,
        workspace_key: str = "default",
        attachment_store: AttachmentStore | None = None,
        clock=time.time,
    ) -> None:
        self.db_path = db_path
        self.store = PracticeStore(db_path)
        self.extractor = extractor
        self.workspace_key = workspace_key or "default"
        self.attachments = attachment_store or get_attachment_store()
        self.clock = clock
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS photo_inbox_items (
                    id TEXT PRIMARY KEY,
                    workspace_key TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    asset_json TEXT NOT NULL,
                    assets_json TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'unclassified',
                    status TEXT NOT NULL DEFAULT 'queued',
                    drafts_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    received_at REAL NOT NULL,
                    source_sent_at REAL,
                    last_received_at REAL NOT NULL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    lease_token TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    entry_ids_json TEXT NOT NULL DEFAULT '[]',
                    import_token TEXT NOT NULL DEFAULT '',
                    archived_at REAL,
                    UNIQUE(workspace_key, source_hash)
                );
                CREATE TABLE IF NOT EXISTS photo_inbox_receipts (
                    event_key TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES photo_inbox_items(id),
                    received_at REAL NOT NULL,
                    source_sent_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_photo_inbox_queue
                    ON photo_inbox_items(workspace_key, status, next_retry_at, received_at);
                CREATE TABLE IF NOT EXISTS photo_question_links (
                    item_id TEXT NOT NULL REFERENCES photo_inbox_items(id),
                    draft_id TEXT NOT NULL,
                    entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
                    PRIMARY KEY(item_id, draft_id)
                );
            """
            )
            # Constructors may run in an API request and the background worker
            # at the same time. Serialize introspection with additive migration.
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(photo_inbox_items)")}
            if "archived_at" not in columns:
                conn.execute("ALTER TABLE photo_inbox_items ADD COLUMN archived_at REAL")

    @staticmethod
    def validate_image(data: bytes, mime_type: str) -> None:
        if not data or len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Choose a non-empty image up to 20 MB")
        allowed = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
        if mime_type not in allowed:
            raise ValueError("Supported photo formats: JPG, PNG and WebP")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != allowed[mime_type]:
                    raise ValueError("The file content does not match its declared type")
                if image.width * image.height > 40_000_000:
                    raise ValueError("Photo exceeds 40 million pixels")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("The image is damaged or unsupported") from exc

    async def _save_asset(self, data: bytes, filename: str, mime_type: str) -> dict:
        asset_id = "photo_" + uuid.uuid4().hex
        filename = Path(filename).name[:180] or "photo"
        url = await self.attachments.put(
            session_id=asset_id,
            attachment_id="original",
            filename=filename,
            data=data,
            mime_type=mime_type,
        )
        return {"id": asset_id, "url": url, "filename": filename, "mime_type": mime_type}

    async def ingest(
        self,
        *,
        data: bytes,
        filename: str,
        mime_type: str,
        source: dict,
        received_at: float,
        source_sent_at: float | None = None,
    ) -> dict:
        event_key = str(source.get("event_key") or "").strip()
        if not event_key or len(event_key) > 1024:
            raise ValueError("A stable channel event key is required")
        if not math.isfinite(received_at) or received_at <= 0:
            raise ValueError("A valid first received time is required")
        if source_sent_at is not None and not math.isfinite(source_sent_at):
            raise ValueError("Invalid source time")
        with self.store.connect() as conn:
            prior = conn.execute(
                "SELECT item_id FROM photo_inbox_receipts WHERE event_key=?", (event_key,)
            ).fetchone()
        if prior:
            return self.detail(prior["item_id"])
        self.validate_image(data, mime_type)
        digest = hashlib.sha256(data).hexdigest()
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM photo_inbox_items WHERE workspace_key=? AND source_hash=?",
                (self.workspace_key, digest),
            ).fetchone()
        asset = None if existing else await self._save_asset(data, filename, mime_type)
        discarded_asset = None
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT item_id FROM photo_inbox_receipts WHERE event_key=?", (event_key,)
            ).fetchone()
            row = conn.execute(
                "SELECT id FROM photo_inbox_items WHERE workspace_key=? AND source_hash=?",
                (self.workspace_key, digest),
            ).fetchone()
            if prior:
                item_id = prior["item_id"]
                discarded_asset = asset
            else:
                item_id = row["id"] if row else "pi_" + uuid.uuid4().hex
                if row:
                    discarded_asset = asset
                    conn.execute(
                        "UPDATE photo_inbox_items SET last_received_at=MAX(last_received_at,?), "
                        "source_sent_at=CASE WHEN received_at>? THEN ? ELSE source_sent_at END, "
                        "archived_at=CASE WHEN archived_at<? THEN NULL ELSE archived_at END, "
                        "received_at=MIN(received_at,?), version=version+1 WHERE id=?",
                        (
                            received_at,
                            received_at,
                            source_sent_at,
                            received_at,
                            received_at,
                            item_id,
                        ),
                    )
                else:
                    assert asset is not None
                    public_source = {
                        key: str(source[key])[:200]
                        for key in _PUBLIC_SOURCE_FIELDS
                        if key in source
                    }
                    conn.execute(
                        """INSERT INTO photo_inbox_items(id,workspace_key,source_hash,
                        asset_json,assets_json,received_at,source_sent_at,last_received_at,source_json)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            item_id,
                            self.workspace_key,
                            digest,
                            json.dumps(asset),
                            json.dumps([asset]),
                            received_at,
                            source_sent_at,
                            received_at,
                            json.dumps(public_source),
                        ),
                    )
                conn.execute(
                    "INSERT INTO photo_inbox_receipts VALUES(?,?,?,?)",
                    (event_key, item_id, received_at, source_sent_at),
                )
        if discarded_asset:
            await self.attachments.delete_session(discarded_asset["id"])
        return self.detail(item_id)

    def _row(self, conn, item_id: str):
        row = conn.execute(
            "SELECT * FROM photo_inbox_items WHERE id=? AND workspace_key=?",
            (item_id, self.workspace_key),
        ).fetchone()
        if row is None:
            raise LookupError("Photo not found")
        return row

    @staticmethod
    def _check_version(row, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise ReviewConflict("Photo changed; reload it before continuing")

    def detail(self, item_id: str) -> dict:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = reconcile_photo_links(conn, self._row(conn, item_id))
            receipts = conn.execute(
                "SELECT received_at,source_sent_at FROM photo_inbox_receipts "
                "WHERE item_id=? ORDER BY received_at",
                (item_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "status": row["status"],
            "subject": row["subject"],
            "received_at": row["received_at"],
            "source_sent_at": row["source_sent_at"],
            "last_received_at": row["last_received_at"],
            "upload_count": len(receipts),
            "asset": json.loads(row["asset_json"]),
            "assets": json.loads(row["assets_json"]),
            "drafts": json.loads(row["drafts_json"]),
            "version": row["version"],
            "error": row["error"],
            "entry_ids": json.loads(row["entry_ids_json"]),
            "note": row["note"],
            "source": json.loads(row["source_json"]),
            "uploads": [dict(item) for item in receipts],
            "archived_at": row["archived_at"],
        }

    def list(
        self,
        *,
        subject: str = "",
        status: str = "",
        q: str = "",
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        clauses, params = ["workspace_key=?"], [self.workspace_key]
        clauses.append("archived_at IS NOT NULL" if status == "removed" else "archived_at IS NULL")
        if subject:
            clauses.append(
                "((json_array_length(drafts_json)=0 AND subject=?) OR EXISTS(SELECT 1 FROM json_each(drafts_json) "
                "WHERE json_extract(value,'$.subject')=?))"
            )
            params.extend([_subject(subject), subject])
        if status == "pending":
            clauses.append("status IN (" + ",".join("?" for _ in PENDING) + ")")
            params.extend(PENDING)
        elif status and status != "removed":
            if status not in (*PENDING, "ready", "confirmed"):
                raise ValueError("Unknown photo status")
            clauses.append("status=?")
            params.append(status)
        if q:
            clauses.append("(drafts_json LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\')")
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        for value, operator in ((since, ">="), (until, "<")):
            if value is not None:
                if not math.isfinite(value):
                    raise ValueError("Invalid date filter")
                clauses.append(f"received_at {operator} ?")
                params.append(value)
        where = " AND ".join(clauses)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for archived in conn.execute(
                "SELECT * FROM photo_inbox_items WHERE workspace_key=? "
                "AND (status='confirmed' OR entry_ids_json!='[]')",
                (self.workspace_key,),
            ).fetchall():
                reconcile_photo_links(conn, archived)
            total = conn.execute(
                f"SELECT COUNT(*) FROM photo_inbox_items WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT id FROM photo_inbox_items WHERE {where} ORDER BY received_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 100)), max(0, offset)],
            ).fetchall()
            counts = conn.execute(
                "SELECT COUNT(*) photos, COALESCE(SUM(status NOT IN ('ready','confirmed')),0) pending, "
                "COALESCE(SUM(status='confirmed'),0) confirmed FROM photo_inbox_items WHERE workspace_key=? AND archived_at IS NULL",
                (self.workspace_key,),
            ).fetchone()
        return {
            "items": [self.detail(row["id"]) for row in rows],
            "total": total,
            "subjects": [{"id": key, "label": label} for key, label in SUBJECTS.items()],
            "counts": dict(counts),
        }

    @staticmethod
    def _edited_drafts(originals: list[dict], edits: list[dict]) -> list[dict]:
        by_id = {item["draft_id"]: item for item in originals}
        if len(edits) != len(originals) or len(edits) > MAX_DRAFTS:
            raise ValueError("Keep every recognized draft; use selected to exclude a question")
        seen, result = set(), []
        for edited in edits:
            draft_id = str(edited.get("draft_id") or "")
            if draft_id not in by_id or draft_id in seen:
                raise ValueError("Unknown or duplicate draft")
            seen.add(draft_id)
            original = by_id[draft_id]
            # Images and locators remain server-owned, including during edits.
            prepared = QuestionRecognitionService._prepare_draft(
                {
                    **edited,
                    "question_images": original["question_images"],
                    "source_locator": original["source_locator"],
                },
                original["ordinal"],
                {},
            )
            prepared["subject"] = _subject(edited.get("subject", original.get("subject")))
            prepared["grade"] = str(edited.get("grade", original.get("grade", "")) or "").strip()[:100]
            prepared["knowledge_point"] = str(
                edited.get("knowledge_point", original.get("knowledge_point", "")) or ""
            ).strip()[:200]
            prepared["selected"] = bool(edited.get("selected", True))
            # Preserve provenance so a client cannot upgrade an invented answer
            # to document evidence by changing an answer_origin field.
            answer = str(edited.get("correct_answer") or "").strip()
            if answer:
                unchanged_document = (
                    original["answer_origin"] == "document" and answer == original["correct_answer"]
                )
                prepared["answer_origin"] = (
                    "document"
                    if unchanged_document
                    else ("user" if edited.get("answer_origin") == "user" else "model_suggested")
                )
            else:
                prepared["answer_origin"] = "missing"
            result.append(prepared)
        return result

    @staticmethod
    def _ready_status(drafts: list[dict]) -> str:
        return (
            "needs_subject"
            if any(item.get("subject") == "unclassified" for item in drafts)
            else "ready"
        )

    def update(
        self,
        item_id: str,
        *,
        expected_version: int,
        subject: str | None = None,
        drafts: list[dict] | None = None,
        note: str | None = None,
    ) -> dict:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, item_id)
            self._check_version(row, expected_version)
            if row["archived_at"] is not None:
                raise ReviewConflict("Restore the photo before editing")
            if row["status"] in {"recognizing", "confirmed"}:
                raise ReviewConflict("This photo cannot be edited in its current state")
            prepared = json.loads(row["drafts_json"])
            if drafts is not None:
                prepared = self._edited_drafts(prepared, drafts)
            new_subject = row["subject"] if subject is None else _subject(subject)
            if subject is not None:
                prepared = [{**item, "subject": new_subject} for item in prepared]
            subjects = {item["subject"] for item in prepared}
            if subject is None and prepared:
                new_subject = next(iter(subjects)) if len(subjects) == 1 else "unclassified"
            status = self._ready_status(prepared) if prepared else row["status"]
            conn.execute(
                "UPDATE photo_inbox_items SET subject=?,drafts_json=?,note=?,status=?,version=version+1 WHERE id=?",
                (
                    new_subject,
                    json.dumps(prepared, ensure_ascii=False),
                    row["note"] if note is None else cell_text(note),
                    status,
                    item_id,
                ),
            )
        return self.detail(item_id)

    def retry(self, item_id: str, *, expected_version: int) -> dict:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, item_id)
            self._check_version(row, expected_version)
            if row["archived_at"] is not None:
                raise ReviewConflict("Restore the photo before retrying")
            if row["status"] not in {"failed", "needs_retake", "non_question"}:
                raise ReviewConflict("Only failed recognition can be retried")
            conn.execute(
                "UPDATE photo_inbox_items SET status='queued',attempts=0,next_retry_at=0,error='',version=version+1 WHERE id=?",
                (item_id,),
            )
        return self.detail(item_id)

    async def supplement(
        self, item_id: str, *, expected_version: int, data: bytes, filename: str, mime_type: str
    ) -> dict:
        current = self.detail(item_id)
        if current["archived_at"] is not None:
            raise ReviewConflict("Restore the photo before adding a supplementary image")
        if current["version"] != expected_version:
            raise ReviewConflict("Photo changed; reload it before continuing")
        if current["status"] not in {"failed", "needs_retake", "non_question"}:
            raise ReviewConflict("Only failed photos need a supplementary image")
        self.validate_image(data, mime_type)
        asset = await self._save_asset(data, filename, mime_type)
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._row(conn, item_id)
                self._check_version(row, expected_version)
                assets = json.loads(row["assets_json"])
                assets.append(asset)
                conn.execute(
                    "UPDATE photo_inbox_items SET assets_json=?,status='queued',attempts=0,"
                    "next_retry_at=0,error='',version=version+1 WHERE id=?",
                    (json.dumps(assets), item_id),
                )
        except Exception:
            await self.attachments.delete_session(asset["id"])
            raise
        return self.detail(item_id)

    def _claim(self) -> dict | None:
        now, token = self.clock(), uuid.uuid4().hex
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM photo_inbox_items WHERE workspace_key=? AND status='recognizing' AND lease_until>?",
                (self.workspace_key, now),
            ).fetchone():
                return None
            # Respect concurrent uploads in the existing recognition importer.
            if conn.execute(
                "SELECT 1 FROM practice_recognition_jobs WHERE workspace_key=? AND expires_at>? "
                "AND status IN ('queued','validating','parsing','recognizing')",
                (self.workspace_key, now),
            ).fetchone():
                return None
            row = conn.execute(
                "SELECT * FROM photo_inbox_items WHERE workspace_key=? AND archived_at IS NULL AND "
                "((status='queued' AND next_retry_at<=?) OR (status='recognizing' AND lease_until<=?)) "
                "ORDER BY received_at,id LIMIT 1",
                (self.workspace_key, now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE photo_inbox_items SET status='recognizing',attempts=attempts+1,lease_until=?,"
                "lease_token=?,version=version+1 WHERE id=?",
                (now + _LEASE_SECONDS, token, row["id"]),
            )
            return {**dict(row), "lease_token": token, "attempts": row["attempts"] + 1}

    async def process_pending(self, *, limit: int = 1) -> int:
        processed = 0
        for _ in range(max(0, limit)):
            row = await asyncio.to_thread(self._claim)
            if row is None:
                break
            try:
                asset = json.loads(row["assets_json"])[-1]
                path = self.attachments.resolve_path(
                    session_id=asset["id"],
                    attachment_id="original",
                    filename=asset["filename"],
                )
                if path is None:
                    raise ValueError("The archived source image is unavailable")
                data = await asyncio.to_thread(path.read_bytes)
                request = StartRecognitionRequest(
                    filename=asset["filename"],
                    mime_type=asset["mime_type"],
                    data=data,
                    workspace_key=self.workspace_key,
                    target="mistakes",
                    source_attachment=asset,
                )
                raw = await asyncio.wait_for(self.extractor.extract(request), _RECOGNITION_TIMEOUT)
                if not isinstance(raw, list) or len(raw) > MAX_DRAFTS:
                    raise ValueError("Recognition produced an invalid question list")
                drafts = []
                for ordinal, item in enumerate(raw, 1):
                    prepared = QuestionRecognitionService._prepare_draft(item, ordinal, asset)
                    # Ignore remote attachment references supplied by the model.
                    prepared["question_images"] = [asset]
                    try:
                        subject = _subject(item.get("subject"))
                    except ValueError:
                        subject = "unclassified"
                    prepared["subject"] = (
                        row["subject"] if row["subject"] != "unclassified" else subject
                    )
                    prepared["selected"] = True
                    drafts.append(prepared)
                status = (
                    (
                        self._ready_status(drafts)
                        if all(item["question"] for item in drafts)
                        else "needs_retake"
                    )
                    if drafts
                    else "non_question"
                )
                subjects = {item["subject"] for item in drafts}
                subject = next(iter(subjects)) if len(subjects) == 1 else "unclassified"
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE photo_inbox_items SET status=?,subject=?,drafts_json=?,error='',"
                        "lease_until=0,lease_token='',version=version+1 WHERE id=? AND lease_token=?",
                        (
                            status,
                            subject,
                            json.dumps(drafts, ensure_ascii=False),
                            row["id"],
                            row["lease_token"],
                        ),
                    )
            except asyncio.CancelledError:
                # Do not erase the persisted lease. A replacement worker resumes it.
                raise
            except Exception:
                # Model/provider exceptions may include tokens or request payloads.
                # Keep the user-facing durable error intentionally bounded and safe.
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE photo_inbox_items SET status=?,error=?,next_retry_at=?,lease_until=0,"
                        "lease_token='',version=version+1 WHERE id=? AND lease_token=?",
                        (
                            "failed" if row["attempts"] >= 3 else "queued",
                            "Recognition failed; retry or add a clearer photo.",
                            self.clock() + 30 * row["attempts"],
                            row["id"],
                            row["lease_token"],
                        ),
                    )
            processed += 1
        return processed

    def confirm(
        self,
        item_id: str,
        *,
        expected_version: int,
        confirmed: bool,
        drafts: list[dict] | None = None,
    ) -> dict:
        if not confirmed:
            raise ValueError("Confirm that the question text and subjects have been checked")
        current = self.detail(item_id)
        if current["archived_at"] is not None:
            raise ReviewConflict("Restore the photo before confirming")
        already_confirmed = current["status"] == "confirmed"
        if not already_confirmed and current["version"] != expected_version:
            raise ReviewConflict("Photo changed; reload it before continuing")
        if current["status"] not in {"ready", "needs_subject", "confirmed"}:
            raise ValueError("Recognition must be ready before confirming")
        if already_confirmed:
            # Idempotent repeats still revalidate live links in commit's write
            # transaction. Ignore stale edits to a completely confirmed archive.
            expected_version, drafts = current["version"], current["drafts"]
        edited = self._edited_drafts(
            current["drafts"], drafts if drafts is not None else current["drafts"]
        )
        questions = []
        for draft in edited:
            if not draft.get("selected", True):
                continue
            if draft["subject"] == "unclassified":
                raise ValueError("Choose a subject for every selected question")
            if draft["correct_answer"] and draft["answer_origin"] not in {"document", "user"}:
                raise ValueError("Answers absent from the photo must be confirmed by the user")
            canonical = normalize_question(draft, require_answer=False)
            canonical.update(
                {
                    "question_images": draft["question_images"],
                    "source_locator": draft["source_locator"],
                    "answer_origin": draft["answer_origin"],
                    "knowledge_point_id": draft.get("knowledge_point", ""),
                    "recognition_meta": {
                        "photo_receipt_id": item_id,
                        "draft_id": draft["draft_id"],
                        "subject": draft["subject"],
                        "grade": draft.get("grade", ""),
                        "photo_source_channel": current.get("source", {}).get("channel", ""),
                        "photo_partner_id": current.get("source", {}).get("partner_id", ""),
                        "first_uploaded_at": current["received_at"],
                        "mistake_origin": "self_reported",
                        "note": current["note"],
                    },
                }
            )
            questions.append(canonical)
        if not questions:
            raise ValueError("Select at least one question")
        token = self.store.stage_import(
            current["asset"]["filename"],
            "mistakes",
            questions,
            photo_context={
                "item_id": item_id,
                "expected_version": expected_version,
                "workspace_key": self.workspace_key,
                "drafts": edited,
            },
        )
        result = self.store.commit_import(token)
        return {**self.detail(item_id), "result": result}

    def set_archived(self, item_id: str, *, expected_version: int, archived: bool) -> dict:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, item_id)
            self._check_version(row, expected_version)
            if (row["archived_at"] is not None) != archived:
                conn.execute(
                    "UPDATE photo_inbox_items SET archived_at=?,version=version+1 WHERE id=?",
                    (self.clock() if archived else None, item_id),
                )
        return self.detail(item_id)


def get_photo_ingestion_service() -> PhotoIngestionService:
    from deeptutor.services.practice.recognition.extractor import LLMImageQuestionExtractor
    from deeptutor.services.session import get_sqlite_session_store
    from deeptutor.services.workspace.context import current_workspace_id

    return PhotoIngestionService(
        get_sqlite_session_store().db_path,
        LLMImageQuestionExtractor(),
        workspace_key=current_workspace_id() or "default",
    )
