"""Authenticated, workspace-scoped photo archive and review endpoints."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from deeptutor.services.practice.photo_ingestion import SUBJECTS, get_photo_ingestion_service
from deeptutor.services.practice.photo_ingestion.service import PENDING
from deeptutor.services.practice.recognition.service import MAX_SOURCE_BYTES
from deeptutor.services.practice.storage import ReviewConflict

router = APIRouter()


class PhotoRevision(BaseModel):
    expected_version: int = Field(ge=1)


class PhotoUpdate(PhotoRevision):
    subject: str | None = Field(default=None, max_length=30)
    drafts: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=20000)


class PhotoConfirm(PhotoRevision):
    confirmed: bool = False
    drafts: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=100)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ReviewConflict):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


async def _account_photo_index(*, limit: int, offset: int, **filters) -> dict:
    """Aggregate only navigation rows; detail and writes retain one scope."""
    from deeptutor.services.workspace.navigation import read_workspace_indexes

    # Navigation tolerates unavailable stores. Invalid request filters must
    # instead fail before that tolerance can turn them into an empty success.
    if filters["subject"] and filters["subject"] not in SUBJECTS:
        raise ValueError("Unknown subject")
    if filters["status"] and filters["status"] not in (
        *PENDING,
        "ready",
        "confirmed",
        "pending",
        "removed",
    ):
        raise ValueError("Unknown photo status")
    if any(
        value is not None and not math.isfinite(value)
        for value in (filters["since"], filters["until"])
    ):
        raise ValueError("Invalid date filter")

    total = 0
    counts = {"photos": 0, "pending": 0, "confirmed": 0}

    async def read():
        nonlocal total
        service = get_photo_ingestion_service()
        rows = []
        first_page = None
        # Each store caps a page at 100. Fetch the necessary prefix in bounded
        # chunks before applying the global offset, including pages after 100.
        for start in range(0, offset + limit, 100):
            size = min(100, offset + limit - start)
            page = await asyncio.to_thread(service.list, **filters, limit=size, offset=start)
            if first_page is None:
                first_page = page
            rows.extend(page["items"])
            if len(page["items"]) < size:
                break
        # Do not contribute partial metadata if a later page could not be read.
        assert first_page is not None
        total += first_page["total"]
        for key in counts:
            counts[key] += first_page["counts"][key]
        return rows

    rows, unavailable = await read_workspace_indexes(read)
    rows.sort(key=lambda row: (-row["received_at"], row["content_workspace_id"], row["id"]))
    return {
        "items": rows[offset : offset + limit],
        "total": total,
        "counts": counts,
        "subjects": [{"id": key, "label": label} for key, label in SUBJECTS.items()],
        "unavailable_workspaces": unavailable,
    }


@router.get("")
async def list_photos(
    subject: str = Query("", max_length=30),
    status: str = Query("", max_length=30),
    q: str = Query("", max_length=200),
    since: float | None = None,
    until: float | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    all_workspaces: bool = False,
):
    try:
        if all_workspaces:
            return await _account_photo_index(
                subject=subject,
                status=status,
                q=q,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        return await asyncio.to_thread(
            get_photo_ingestion_service().list,
            subject=subject,
            status=status,
            q=q,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.get("/{item_id}")
async def photo_detail(item_id: str):
    try:
        return await asyncio.to_thread(get_photo_ingestion_service().detail, item_id)
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.patch("/{item_id}")
async def update_photo(item_id: str, payload: PhotoUpdate):
    try:
        return await asyncio.to_thread(
            get_photo_ingestion_service().update,
            item_id,
            **payload.model_dump(),
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.post("/{item_id}/retry")
async def retry_photo(item_id: str, payload: PhotoRevision):
    try:
        return await asyncio.to_thread(
            get_photo_ingestion_service().retry,
            item_id,
            expected_version=payload.expected_version,
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.post("/{item_id}/remove")
async def remove_photo(item_id: str, payload: PhotoRevision):
    try:
        return await asyncio.to_thread(
            get_photo_ingestion_service().set_archived,
            item_id,
            expected_version=payload.expected_version,
            archived=True,
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.post("/{item_id}/restore")
async def restore_photo(item_id: str, payload: PhotoRevision):
    try:
        return await asyncio.to_thread(
            get_photo_ingestion_service().set_archived,
            item_id,
            expected_version=payload.expected_version,
            archived=False,
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.post("/{item_id}/confirm")
async def confirm_photo(item_id: str, payload: PhotoConfirm):
    try:
        return await asyncio.to_thread(
            get_photo_ingestion_service().confirm,
            item_id,
            **payload.model_dump(),
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc


@router.post("/{item_id}/supplement")
async def supplement_photo(
    item_id: str,
    expected_version: int = Form(..., ge=1),
    file: UploadFile = File(...),
):
    try:
        data = await file.read(MAX_SOURCE_BYTES + 1)
        # Generic browser MIME values can be inferred from magic bytes;
        # explicit conflicting types still fail full image validation.
        mime = file.content_type or ""
        if mime in {"", "application/octet-stream"}:
            if data.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "image/webp"
        return await get_photo_ingestion_service().supplement(
            item_id,
            expected_version=expected_version,
            data=data,
            filename=file.filename or "photo",
            mime_type=mime,
        )
    except (ValueError, LookupError) as exc:
        raise _http_error(exc) from exc
    finally:
        await file.close()
