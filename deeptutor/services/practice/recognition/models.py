"""Public values for the question-recognition module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RecognitionStatus = Literal[
    "queued",
    "validating",
    "parsing",
    "recognizing",
    "ready",
    "staged",
    "committed",
    "failed",
    "expired",
]


@dataclass(frozen=True)
class StartRecognitionRequest:
    filename: str
    mime_type: str
    data: bytes
    workspace_key: str
    target: Literal["bank", "mistakes"] = "bank"
    course_id: str = ""
    source_attachment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StageDraftsRequest:
    expected_version: int
    drafts: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RecognitionJobRef:
    job_id: str
    status: RecognitionStatus
    created_at: float


__all__ = [
    "RecognitionJobRef",
    "RecognitionStatus",
    "StageDraftsRequest",
    "StartRecognitionRequest",
]
