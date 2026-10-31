"""Practice workflow. Every operation resolves the current user's scoped store."""

from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import Any, Literal
import zipfile

from defusedxml.ElementTree import ParseError
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from deeptutor.services.practice.answers import check_answer
from deeptutor.services.practice.importing import MAX_BYTES, preview
from deeptutor.services.practice.recognition import (
    QuestionRecognitionService,
    StageDraftsRequest,
    StartRecognitionRequest,
)
from deeptutor.services.practice.recognition.extractor import LLMImageQuestionExtractor
from deeptutor.services.practice.recognition.service import MAX_SOURCE_BYTES
from deeptutor.services.practice.scheduler import Rating, day_bounds
from deeptutor.services.practice.storage import PracticeStore, ReviewConflict, ReviewScope
from deeptutor.services.session import get_sqlite_session_store
from deeptutor.services.workspace.context import current_workspace_id

from .photo_inbox import router as photo_inbox_router
from .question_notebook import NotebookEntryItem, _course_session_ids

router = APIRouter()
router.include_router(photo_inbox_router, prefix="/photo-inbox")


class PracticeSummary(BaseModel):
    total: int
    mistakes: int
    due: int
    overdue: int
    reviewed_today: int
    next_due_at: float | None
    day_end: float
    timezone: str
    open: int = 0
    verify: int = 0
    closed: int = 0
    paused: int = 0
    unavailable_workspaces: list[str] = Field(default_factory=list)


class RecommendationMetadata(BaseModel):
    reasons: list[str] = Field(default_factory=list)
    score: float = 0
    estimated_minutes: int = 1


class PracticeQuestion(BaseModel):
    entry: NotebookEntryItem
    state: dict
    content_workspace_id: str = ""
    content_workspace_name: str = ""
    recommendation: RecommendationMetadata | None = None


class PracticeRecommendations(BaseModel):
    items: list[PracticeQuestion]
    estimated_minutes: int
    budget_minutes: int
    unavailable_workspaces: list[str] = Field(default_factory=list)


class MasteryDetails(BaseModel):
    state: dict[str, Any]
    evidence: dict[str, Any]
    history: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class PracticeCounts(BaseModel):
    questions: int
    mistakes: int
    reviews: int


class PracticeDailyCounts(PracticeCounts):
    date: str


class PracticeSourceCounts(PracticeCounts):
    source: str


class PracticeAnalytics(BaseModel):
    timezone: str
    days: int
    start_date: str
    end_date: str
    updated_at: float
    daily: list[PracticeDailyCounts]
    sources: list[PracticeSourceCounts]
    totals: PracticeCounts


class AnswerRequest(BaseModel):
    answer: str = Field(default="", max_length=20000)


class ReviewRequest(AnswerRequest):
    self_report: bool = False
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    version: int = Field(ge=0)
    rating: Rating
    auto_grade: bool = False
    hint_used: bool = False
    answer_revealed: bool = False
    attempt_kind: Literal["original", "spotcheck"] = "original"
    timezone: str = Field(default="UTC", max_length=100)
    review_scope: ReviewScope = "mistakes"


class ExposureRequest(BaseModel):
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["hint", "answer"]


class ExposureResult(BaseModel):
    entry_id: int
    kind: Literal["hint", "answer"]
    exposed_at: float


class LearningUpdateRequest(BaseModel):
    version: int = Field(ge=0)
    paused: bool


class VariantGenerateRequest(BaseModel):
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["similar", "advanced"]


class VariantDraft(BaseModel):
    draft_id: str
    version: int
    parent_entry_id: int
    kind: Literal["similar", "advanced"]
    status: Literal["ready", "confirmed"]
    question: str
    question_type: str
    options: dict[str, str]
    correct_answer: str
    explanation: str
    difficulty: str
    knowledge_point: str
    target_skill: str
    introduces_new_skill: bool
    entry_id: int | None = None


class VariantConfirmRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    confirmed: bool = False
    question: dict[str, Any]


class CommitRequest(BaseModel):
    token: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$")


class RecognitionStageRequest(BaseModel):
    expected_version: int = Field(ge=1)
    drafts: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def _recognition_service() -> QuestionRecognitionService:
    return QuestionRecognitionService(
        get_sqlite_session_store().db_path,
        LLMImageQuestionExtractor(),
    )


def _recognition_mime(declared: str, data: bytes) -> str:
    """Use magic bytes for generic browser uploads; reject conflicting types later."""

    if declared in {"image/jpeg", "image/png", "image/webp", "application/pdf"}:
        return declared
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return declared


def _validate_timezone(timezone: str) -> tuple[float, float]:
    try:
        return day_bounds(timezone, time.time())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _entry(entry_id: int) -> dict:
    store = get_sqlite_session_store()
    if not await asyncio.to_thread(PracticeStore(store.db_path).is_visible, entry_id):
        raise HTTPException(404, "Question not found")
    entry = await store.get_notebook_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "Question not found")
    return entry


@router.get("/summary", response_model=PracticeSummary)
async def summary(
    timezone: str = "UTC",
    course_id: str = "",
    all_workspaces: bool = False,
    review_scope: ReviewScope = "mistakes",
):
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        _, end = _validate_timezone(timezone)

        async def read():
            return [await summary(timezone, course_id, review_scope=review_scope)]

        rows, unavailable = await read_workspace_indexes(read)
        return {
            **{
                key: sum(row.get(key, 0) for row in rows)
                for key in (
                    "total",
                    "mistakes",
                    "due",
                    "overdue",
                    "reviewed_today",
                    "open",
                    "verify",
                    "closed",
                    "paused",
                )
            },
            "next_due_at": min(
                (row["next_due_at"] for row in rows if row["next_due_at"]), default=None
            ),
            "day_end": end,
            "timezone": timezone,
            "unavailable_workspaces": unavailable,
        }

    store = get_sqlite_session_store()
    sessions = await _course_session_ids(store, course_id)
    try:
        return await asyncio.to_thread(
            PracticeStore(store.db_path).overview,
            timezone,
            sessions,
            course_id=course_id,
            review_scope=review_scope,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/queue", response_model=list[PracticeQuestion])
async def queue(
    timezone: str = "UTC",
    course_id: str = "",
    category_id: int | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    all_workspaces: bool = False,
    review_scope: ReviewScope = "mistakes",
):
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        _validate_timezone(timezone)

        async def read():
            return await queue(timezone, course_id, category_id, limit, review_scope=review_scope)

        rows, _ = await read_workspace_indexes(read)
        rows.sort(
            key=lambda row: (
                row["state"].get("due_at") or row["entry"].get("created_at", 0),
                row["content_workspace_id"],
                row["entry"]["id"],
            )
        )
        return rows[:limit]

    store = get_sqlite_session_store()
    practice = PracticeStore(store.db_path)
    sessions = await _course_session_ids(store, course_id)
    try:
        ids = await asyncio.to_thread(
            practice.queue,
            timezone,
            sessions,
            category_id,
            limit,
            course_id=course_id,
            review_scope=review_scope,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    from deeptutor.services.workspace.context import current_workspace_id

    questions = []
    for entry_id in ids:
        entry = await store.get_notebook_entry(entry_id)
        if entry:
            questions.append(
                {
                    "entry": entry,
                    "state": await asyncio.to_thread(practice.state, entry_id),
                    "content_workspace_id": current_workspace_id(),
                }
            )
    return questions


@router.get("/recommendations", response_model=PracticeRecommendations)
async def recommendations(
    timezone: str = "UTC",
    course_id: str = "",
    category_id: int | None = None,
    budget_minutes: int = Query(default=15, ge=5, le=60),
    subject: str = Query(default="", max_length=100),
    query: str = Query(default="", max_length=200),
    include_early: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    all_workspaces: bool = False,
    grade: str = Query(default="", max_length=100),
):
    _validate_timezone(timezone)
    unavailable = []
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        async def read():
            # Gather candidates before applying the account-wide time budget.
            result = await recommendations(
                timezone=timezone,
                course_id=course_id,
                category_id=category_id,
                budget_minutes=60,
                subject=subject,
                query=query,
                include_early=include_early,
                limit=100,
                grade=grade,
            )
            return result["items"]

        rows, unavailable = await read_workspace_indexes(read)
    else:
        store = get_sqlite_session_store()
        practice = PracticeStore(store.db_path)
        sessions = await _course_session_ids(store, course_id)
        ranked = await asyncio.to_thread(
            practice.recommendations,
            timezone,
            sessions,
            course_id=course_id,
            category_id=category_id,
            budget_minutes=budget_minutes,
            subject=subject,
            grade=grade,
            query=query,
            include_early=include_early,
            limit=limit,
        )
        rows = []
        for recommendation in ranked:
            entry_id = recommendation["entry_id"]
            entry = await store.get_notebook_entry(entry_id)
            if entry:
                rows.append(
                    {
                        "entry": entry,
                        "state": await asyncio.to_thread(practice.state, entry_id),
                        "content_workspace_id": current_workspace_id(),
                        "recommendation": {
                            key: recommendation[key]
                            for key in ("score", "reasons", "estimated_minutes")
                        },
                    }
                )
    chosen, minutes, topics = [], 0, {}
    while rows and len(chosen) < limit:
        if all_workspaces and not course_id:
            # Apply diversity once across the account, rather than sorting
            # away the per-workspace ranker's topic penalty.
            def priority(row):
                entry = row["entry"]
                topic = entry.get("knowledge_point_id") or entry.get("section_title") or ""
                return (
                    -(row["recommendation"]["score"] - 12 * topics.get(topic, 0)),
                    row.get("content_workspace_id", ""),
                    entry["id"],
                )

            rows.sort(key=priority)
        row = rows.pop(0)
        duration = row["recommendation"]["estimated_minutes"]
        if minutes + duration <= budget_minutes:
            chosen.append(row)
            minutes += duration
            topic = (
                row["entry"].get("knowledge_point_id") or row["entry"].get("section_title") or ""
            )
            topics[topic] = topics.get(topic, 0) + 1
    return {
        "items": chosen,
        "estimated_minutes": minutes,
        "budget_minutes": budget_minutes,
        "unavailable_workspaces": unavailable,
    }


@router.get("/analytics", response_model=PracticeAnalytics)
async def practice_analytics(
    timezone: str = "UTC", days: int = 30, course_id: str = "", all_workspaces: bool = False
):
    _validate_timezone(timezone)
    if days not in {7, 30, 90}:
        raise HTTPException(422, "Days must be 7, 30, or 90")
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        async def read():
            return [await practice_analytics(timezone, days, course_id)]

        rows, _ = await read_workspace_indexes(read)
        if not rows:
            return await practice_analytics(timezone, days, course_id)
        result = dict(rows[0])
        for bucket, identity in (("daily", "date"), ("sources", "source")):
            combined = {}
            for row in rows:
                for item in row[bucket]:
                    target = combined.setdefault(
                        item[identity],
                        {identity: item[identity], "questions": 0, "mistakes": 0, "reviews": 0},
                    )
                    for metric in ("questions", "mistakes", "reviews"):
                        target[metric] += item[metric]
            result[bucket] = list(combined.values())
        result["totals"] = {
            metric: sum(row["totals"][metric] for row in rows)
            for metric in ("questions", "mistakes", "reviews")
        }
        return result

    store = get_sqlite_session_store()
    sessions = await _course_session_ids(store, course_id)
    try:
        return await asyncio.to_thread(
            PracticeStore(store.db_path).analytics,
            timezone,
            days,
            sessions,
            course_id=course_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/questions/{entry_id}", response_model=PracticeQuestion)
async def practice_question(entry_id: int):
    entry = await _entry(entry_id)
    state = await asyncio.to_thread(
        PracticeStore(get_sqlite_session_store().db_path).state, entry_id
    )
    return {"entry": entry, "state": state, "content_workspace_id": current_workspace_id()}


@router.get("/questions/{entry_id}/mastery", response_model=MasteryDetails)
async def mastery_details(entry_id: int):
    await _entry(entry_id)
    return await asyncio.to_thread(
        PracticeStore(get_sqlite_session_store().db_path).mastery_details, entry_id
    )


@router.post("/questions/{entry_id}/exposure", response_model=ExposureResult)
async def record_exposure(entry_id: int, payload: ExposureRequest):
    await _entry(entry_id)
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).expose,
            entry_id,
            payload.request_id,
            payload.kind,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/questions/{entry_id}/learning")
async def update_learning(entry_id: int, payload: LearningUpdateRequest):
    await _entry(entry_id)
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).update_learning,
            entry_id,
            payload.version,
            payload.paused,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/questions/{entry_id}/check")
async def check(entry_id: int, payload: AnswerRequest):
    entry = await _entry(entry_id)
    return {"correct": check_answer(entry, payload.answer)}


@router.post("/questions/{entry_id}/review")
async def review(entry_id: int, payload: ReviewRequest):
    _validate_timezone(payload.timezone)
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).review,
            entry_id,
            payload.request_id,
            payload.version,
            payload.rating,
            payload.answer,
            self_report=payload.self_report,
            auto_grade=payload.auto_grade,
            hint_used=payload.hint_used,
            answer_revealed=payload.answer_revealed,
            attempt_kind=payload.attempt_kind,
            timezone=payload.timezone,
            review_scope=payload.review_scope,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _variant_service():
    from deeptutor.services.practice.variants import VariantService

    return VariantService(get_sqlite_session_store().db_path)


@router.post("/questions/{entry_id}/variants/generate", response_model=VariantDraft)
async def generate_variant(entry_id: int, payload: VariantGenerateRequest):
    await _entry(entry_id)
    try:
        return await _variant_service().generate(entry_id, payload.request_id, payload.kind)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/questions/{entry_id}/variants/confirm", response_model=PracticeQuestion)
async def confirm_variant(entry_id: int, payload: VariantConfirmRequest):
    await _entry(entry_id)
    try:
        child_id = await asyncio.to_thread(
            _variant_service().confirm,
            entry_id,
            payload.draft_id,
            payload.expected_version,
            payload.question,
            confirmed=payload.confirmed,
        )
        return await practice_question(child_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/import/template")
async def import_template(format: Literal["csv", "xlsx"] = "csv"):
    rows = [
        [
            "question",
            "question_type",
            "A",
            "B",
            "C",
            "D",
            "correct_answer",
            "explanation",
            "tags",
            "user_answer",
        ],
        ["2 + 2 = ?", "single_choice", "3", "4", "5", "6", "B", "2 + 2 = 4", "Math,Addition", ""],
        [
            "Select the prime numbers",
            "multi_choice",
            "2",
            "3",
            "4",
            "6",
            "A,B",
            "2 and 3 are prime",
            "Math",
            "",
        ],
        ["The Earth orbits the Sun", "true_false", "", "", "", "", "true", "", "Science", ""],
        [
            "Explain photosynthesis",
            "short_answer",
            "",
            "",
            "",
            "",
            "Plants convert light into chemical energy.",
            "",
            "Biology",
            "",
        ],
    ]
    if format == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        book.close()
        data = output.getvalue()
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        data, media = output.getvalue().encode("utf-8-sig"), "text/csv"
    return Response(
        data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="practice-template.{format}"'},
    )


@router.post("/import/preview")
async def import_preview(
    file: UploadFile = File(...),
    target: Literal["bank", "mistakes"] = Form("bank"),
    course_id: str = Form("", max_length=200),
):
    await _course_session_ids(get_sqlite_session_store(), course_id)
    try:
        data = await file.read(MAX_BYTES + 1)
        parsed = await asyncio.to_thread(preview, data, file.filename or "")
    except (ValueError, UnicodeError, OSError, zipfile.BadZipFile, KeyError, ParseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()
    token = None
    if not parsed["errors"]:
        token = await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).stage_import,
            file.filename or "",
            target,
            parsed["questions"],
            course_id,
        )
    return {
        "token": token,
        "total": len(parsed["questions"]) + len(parsed["errors"]),
        "valid": len(parsed["questions"]),
        "errors": parsed["errors"],
        "samples": parsed["questions"][:5],
    }


@router.post("/import/recognize", status_code=202)
async def start_recognition_import(
    file: UploadFile = File(...),
    target: Literal["bank", "mistakes"] = Form("bank"),
    course_id: str = Form("", max_length=200),
):
    """Start image or PDF recognition without changing the question bank."""

    store = get_sqlite_session_store()
    await _course_session_ids(store, course_id)
    try:
        data = await file.read(MAX_SOURCE_BYTES + 1)
        mime_type = _recognition_mime(file.content_type or "", data)
        ref = await _recognition_service().start(
            StartRecognitionRequest(
                filename=file.filename or "upload",
                mime_type=mime_type,
                data=data,
                workspace_key=current_workspace_id() or "default",
                target=target,
                course_id=course_id,
            )
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()
    return {
        "job_id": ref.job_id,
        "status": ref.status,
        "created_at": ref.created_at,
    }


@router.get("/import/recognize")
async def latest_recognition_import():
    """Return the current running or reviewable recognition job, if any."""

    return await asyncio.to_thread(
        _recognition_service().latest,
        current_workspace_id() or "default",
    )


@router.get("/import/recognize/{job_id}")
async def recognition_import_status(job_id: str):
    try:
        return await asyncio.to_thread(
            _recognition_service().snapshot,
            job_id,
            current_workspace_id() or "default",
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/import/recognize/{job_id}/stage")
async def stage_recognition_import(job_id: str, payload: RecognitionStageRequest):
    try:
        return await asyncio.to_thread(
            _recognition_service().stage,
            job_id,
            current_workspace_id() or "default",
            StageDraftsRequest(
                expected_version=payload.expected_version,
                drafts=payload.drafts,
            ),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status = 409 if "changed" in message else 422
        raise HTTPException(status, message) from exc


@router.post("/import/commit")
async def import_commit(payload: CommitRequest):
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).commit_import, payload.token
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
