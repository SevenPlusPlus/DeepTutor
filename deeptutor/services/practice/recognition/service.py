"""Orchestrate non-deterministic recognition before deterministic importing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import time
from typing import Protocol
import uuid

from deeptutor.services.practice.normalization import TYPES, normalize_question
from deeptutor.services.practice.scheduler import DAY
from deeptutor.services.practice.storage import PracticeStore
from deeptutor.services.storage import AttachmentStore, get_attachment_store

from .models import RecognitionJobRef, StageDraftsRequest, StartRecognitionRequest
from .pdf import render_pdf_pages
from .repository import RecognitionJobRepository

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_DRAFTS = 100
ALLOWED_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
    }
)


class VisionQuestionExtractor(Protocol):
    """Internal seam with production and fake adapters."""

    async def extract(self, request: StartRecognitionRequest) -> list[dict]: ...


class QuestionRecognitionService:
    """Small interface hiding recognition state, validation and import staging."""

    def __init__(
        self,
        db_path: Path,
        extractor: VisionQuestionExtractor,
        *,
        attachment_store: AttachmentStore | None = None,
        clock=time.time,
    ) -> None:
        self.db_path = db_path
        self.extractor = extractor
        self.attachment_store = attachment_store or get_attachment_store()
        self.clock = clock
        self.jobs = RecognitionJobRepository(db_path, clock=clock)
        self._tasks: set[asyncio.Task] = set()

    async def start(self, request: StartRecognitionRequest) -> RecognitionJobRef:
        self._validate_start(request)
        now = self.clock()
        job_id = "rec_" + uuid.uuid4().hex
        self.jobs.create(
            {
                "id": job_id,
                "workspace_key": request.workspace_key,
                "filename": request.filename,
                "mime_type": request.mime_type,
                "source_hash": hashlib.sha256(request.data).hexdigest(),
                "target": request.target,
                "course_id": request.course_id.strip(),
                "created_at": now,
                "expires_at": now + DAY,
            }
        )
        owner_id = f"practice-recognition-{job_id}"
        try:
            source_url = await self.attachment_store.put(
                session_id=owner_id,
                attachment_id="source",
                filename=request.filename,
                data=request.data,
                mime_type=request.mime_type,
            )
            source_attachment = {
                "id": "source",
                "url": source_url,
                "filename": request.filename,
                "mime_type": request.mime_type,
            }
            self.jobs.set_source_attachment(job_id, request.workspace_key, source_attachment)
            request = replace(request, source_attachment=source_attachment)
        except Exception as exc:
            self.jobs.fail(
                job_id,
                request.workspace_key,
                "attachment_store_failed",
                str(exc),
            )
            raise ValueError("Failed to save the source image") from exc
        task = asyncio.create_task(self._recognize(job_id, owner_id, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return RecognitionJobRef(job_id=job_id, status="queued", created_at=now)

    def snapshot(self, job_id: str, workspace_key: str) -> dict:
        job = self._job(job_id, workspace_key)
        return self._public_snapshot(job)

    def latest(self, workspace_key: str) -> dict | None:
        job = self.jobs.latest_recoverable(workspace_key)
        return self._public_snapshot(job) if job is not None else None

    @staticmethod
    def _public_snapshot(job: dict) -> dict:
        return {
            "job_id": job["id"],
            "filename": job["filename"],
            "target": job["target"],
            "status": job["status"],
            "stage": job["stage"],
            "progress_message": job["progress_message"],
            "completed_units": job["completed_units"],
            "total_units": job["total_units"],
            "version": job["version"],
            "drafts": job["drafts"],
            "summary": job["summary"],
            "error_code": job["error_code"],
            "error_message": job["error_message"],
            "expires_at": job["expires_at"],
        }

    def _job(self, job_id: str, workspace_key: str) -> dict:
        job = self.jobs.get(job_id, workspace_key)
        if job is None:
            raise LookupError("Recognition job not found")
        return job

    def stage(
        self,
        job_id: str,
        workspace_key: str,
        request: StageDraftsRequest,
    ) -> dict:
        job = self._job(job_id, workspace_key)
        if job["status"] == "expired":
            raise ValueError("Recognition job expired")
        if job["status"] != "ready":
            raise ValueError("Recognition job is not ready")
        if job["version"] != request.expected_version:
            raise ValueError("Recognition job changed; reload it before importing")

        originals = {str(item.get("draft_id") or ""): item for item in job["drafts"]}
        questions: list[dict] = []
        seen: set[str] = set()
        for edited in request.drafts:
            draft_id = str(edited.get("draft_id") or "")
            if not draft_id or draft_id in seen or draft_id not in originals:
                raise ValueError("Draft selection contains an unknown or duplicate draft")
            seen.add(draft_id)
            if not bool(edited.get("selected", True)):
                continue
            original = originals[draft_id]
            answer = str(edited.get("correct_answer") or edited.get("answer") or "").strip()
            answer_origin = str(edited.get("answer_origin") or original.get("answer_origin") or "")
            original_origin = str(original.get("answer_origin") or "missing")
            if not answer:
                answer_origin = "missing"
            elif original_origin in {"missing", "model_suggested"} and answer_origin != "user":
                raise ValueError("Answers absent from the document must be confirmed by the user")
            elif answer_origin not in {"document", "user"}:
                raise ValueError("Every imported answer must come from the document or the user")

            canonical = normalize_question(edited, require_answer=False)
            canonical.update(
                {
                    "question_images": original.get("question_images", []),
                    "source_locator": original.get("source_locator", {}),
                    "answer_origin": answer_origin,
                    "recognition_meta": {
                        "job_id": job_id,
                        "draft_id": draft_id,
                        "warnings": original.get("warnings", []),
                    },
                }
            )
            questions.append(canonical)
        if not questions:
            raise ValueError("Select at least one valid question to import")

        token = PracticeStore(self.db_path).stage_import(
            job["filename"],
            job["target"],
            questions,
            job["course_id"],
        )
        self.jobs.staged(job_id, workspace_key, request.expected_version, token)
        return {
            "token": token,
            "total": len(request.drafts),
            "valid": len(questions),
            "errors": [],
            "samples": questions[:5],
        }

    @staticmethod
    def _validate_start(request: StartRecognitionRequest) -> None:
        if not request.filename.strip():
            raise ValueError("A source filename is required")
        if not request.data or len(request.data) > MAX_SOURCE_BYTES:
            raise ValueError("Choose a non-empty image or PDF up to 20 MB")
        if request.mime_type not in ALLOWED_MIME_TYPES:
            raise ValueError("Supported formats: JPG, PNG, WebP and PDF")
        signatures = {
            "image/jpeg": request.data.startswith(b"\xff\xd8\xff"),
            "image/png": request.data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": request.data.startswith(b"RIFF")
            and request.data[8:12] == b"WEBP",
            "application/pdf": request.data.startswith(b"%PDF-"),
        }
        if not signatures[request.mime_type]:
            raise ValueError("The file content does not match its declared type")
        if request.target not in {"bank", "mistakes"}:
            raise ValueError("Unknown import target")
        if not request.workspace_key.strip():
            raise ValueError("A workspace scope is required")

    async def _recognize(
        self,
        job_id: str,
        owner_id: str,
        request: StartRecognitionRequest,
    ) -> None:
        try:
            self.jobs.progress(
                job_id,
                request.workspace_key,
                status="validating",
                stage="validating",
                message="Validating source",
            )
            if request.mime_type == "application/pdf":
                raw_drafts = await self._extract_pdf(job_id, owner_id, request)
            else:
                self.jobs.progress(
                    job_id,
                    request.workspace_key,
                    status="recognizing",
                    stage="recognizing",
                    message="Recognizing questions",
                )
                raw_drafts = await self.extractor.extract(request)
            if not isinstance(raw_drafts, list):
                raise ValueError("Question extractor returned an invalid result")
            if len(raw_drafts) > MAX_DRAFTS:
                raise ValueError("Recognition produced more than 100 questions")
            drafts = [
                self._prepare_draft(raw, index, request.source_attachment)
                for index, raw in enumerate(raw_drafts, 1)
            ]
            self.jobs.ready(job_id, request.workspace_key, drafts)
        except Exception as exc:
            self.jobs.fail(job_id, request.workspace_key, "recognition_failed", str(exc))

    async def _extract_pdf(
        self,
        job_id: str,
        owner_id: str,
        request: StartRecognitionRequest,
    ) -> list[dict]:
        self.jobs.progress(
            job_id,
            request.workspace_key,
            status="parsing",
            stage="parsing",
            message="Rendering PDF pages",
        )
        pages = await asyncio.to_thread(render_pdf_pages, request.data)
        stem = Path(request.filename).stem or "document"
        drafts: list[dict] = []
        for index, page in enumerate(pages):
            self.jobs.progress(
                job_id,
                request.workspace_key,
                status="recognizing",
                stage="recognizing",
                message=f"Recognizing PDF page {page.page_number} of {len(pages)}",
                completed_units=index,
                total_units=len(pages),
            )
            attachment_id = f"page-{page.page_number:03d}"
            page_filename = f"{stem}-page-{page.page_number:03d}.png"
            page_url = await self.attachment_store.put(
                session_id=owner_id,
                attachment_id=attachment_id,
                filename=page_filename,
                data=page.data,
                mime_type="image/png",
            )
            page_attachment = {
                "id": attachment_id,
                "url": page_url,
                "filename": page_filename,
                "mime_type": "image/png",
            }
            page_request = replace(
                request,
                filename=page_filename,
                mime_type="image/png",
                data=page.data,
                source_attachment=page_attachment,
            )
            page_drafts = await self.extractor.extract(page_request)
            if not isinstance(page_drafts, list):
                raise ValueError("Question extractor returned an invalid result")
            for draft_index, draft in enumerate(page_drafts, 1):
                if not isinstance(draft, dict):
                    raise ValueError("Question extractor returned a non-object draft")
                locator = draft.get("source_locator")
                if not isinstance(locator, dict):
                    locator = {}
                drafts.append(
                    {
                        **draft,
                        "draft_id": f"p{page.page_number:03d}_q{draft_index:03d}",
                        "question_images": [page_attachment],
                        "source_locator": {**locator, "page_numbers": [page.page_number]},
                    }
                )
                if len(drafts) > MAX_DRAFTS:
                    raise ValueError("Recognition produced more than 100 questions")
            self.jobs.progress(
                job_id,
                request.workspace_key,
                status="recognizing",
                stage="recognizing",
                message=f"Recognized PDF page {page.page_number} of {len(pages)}",
                completed_units=index + 1,
                total_units=len(pages),
            )
        return drafts

    @staticmethod
    def _prepare_draft(
        raw: dict,
        ordinal: int,
        source_attachment: dict[str, str],
    ) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("Question extractor returned a non-object draft")
        question = str(raw.get("question") or "").strip()
        answer = str(raw.get("correct_answer") or raw.get("answer") or "").strip()
        origin = str(raw.get("answer_origin") or "").strip()
        if origin not in {"document", "user", "model_suggested", "missing"}:
            # An answer without explicit provenance is never trusted as source
            # evidence; this keeps a model omission from silently fabricating it.
            origin = "model_suggested" if answer else "missing"
        warnings = [str(item) for item in raw.get("warnings", []) if str(item).strip()]
        normalized: dict
        try:
            normalized = normalize_question(raw, require_answer=False)
        except (TypeError, ValueError) as exc:
            normalized = {
                "question": question,
                "question_type": TYPES.get(
                    str(raw.get("question_type") or "").strip().lower(), "short_answer"
                ),
                "options": raw.get("options") if isinstance(raw.get("options"), dict) else {},
                "correct_answer": answer,
                "explanation": str(raw.get("explanation") or "").strip(),
                "difficulty": str(raw.get("difficulty") or "").strip(),
                "tags": raw.get("tags") if isinstance(raw.get("tags"), list) else [],
            }
            warnings.append(str(exc))
        review_level = "normal"
        if warnings:
            review_level = "review"
        if origin == "missing":
            warnings.append("No answer was found; this question can still be imported.")
            review_level = "review"
        if not question or origin == "model_suggested":
            review_level = "required"
        question_images = raw.get("question_images", [])
        if not isinstance(question_images, list):
            question_images = []
        if (
            not question_images
            and source_attachment
            and str(source_attachment.get("mime_type") or "").startswith("image/")
        ):
            # MVP: preserve the full source image for every extracted question.
            # A later cropper can replace this reference with per-question regions.
            question_images = [source_attachment]
        return {
            "draft_id": str(raw.get("draft_id") or f"q_{ordinal:03d}"),
            "ordinal": ordinal,
            **{key: normalized[key] for key in (
                "question",
                "question_type",
                "options",
                "correct_answer",
                "explanation",
                "difficulty",
                "tags",
            )},
            "question_images": question_images,
            "source_locator": raw.get("source_locator", {}),
            "answer_origin": origin,
            "review_level": review_level,
            "warnings": warnings,
        }


__all__ = ["QuestionRecognitionService", "VisionQuestionExtractor"]
