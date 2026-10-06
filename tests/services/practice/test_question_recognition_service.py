from __future__ import annotations

import asyncio
import io

from pypdf import PdfReader, PdfWriter
import pytest
from reportlab.pdfgen import canvas

from deeptutor.services.practice.recognition import (
    QuestionRecognitionService,
    StageDraftsRequest,
    StartRecognitionRequest,
)
from deeptutor.services.practice.recognition.pdf import render_pdf_pages
from deeptutor.services.practice.storage import PracticeStore
from deeptutor.services.session.sqlite_store import SQLiteSessionStore
from deeptutor.services.storage.attachment_store import LocalDiskAttachmentStore


class FakeExtractor:
    def __init__(self, drafts: list[dict]):
        self.drafts = drafts
        self.requests: list[StartRecognitionRequest] = []

    async def extract(self, request: StartRecognitionRequest) -> list[dict]:
        self.requests.append(request)
        await asyncio.sleep(0)
        return self.drafts


def make_pdf(*pages: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792))
    for text in pages:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return output.getvalue()


async def wait_ready(
    service: QuestionRecognitionService,
    job_id: str,
    workspace_key: str = "workspace-a",
) -> dict:
    for _ in range(100):
        snapshot = service.snapshot(job_id, workspace_key)
        if snapshot["status"] in {"ready", "failed"}:
            return snapshot
        await asyncio.sleep(0)
    raise AssertionError("recognition job did not finish")


@pytest.mark.asyncio
async def test_recognition_stages_and_commits_into_explicit_course(tmp_path):
    store = SQLiteSessionStore(tmp_path / "recognition.db")
    attachments = LocalDiskAttachmentStore(tmp_path / "attachments")
    extractor = FakeExtractor(
        [
            {
                "question": "2 + 2 = ?",
                "question_type": "single_choice",
                "options": {"A": "3", "B": "4"},
                "correct_answer": "B",
                "explanation": "2 + 2 = 4",
                "tags": ["Math"],
                "answer_origin": "document",
                "source_locator": {"page_numbers": [1]},
            }
        ]
    )
    service = QuestionRecognitionService(
        store.db_path,
        extractor,
        attachment_store=attachments,
    )
    ref = await service.start(
        StartRecognitionRequest(
            filename="page.png",
            mime_type="image/png",
            data=b"\x89PNG\r\n\x1a\nimage-bytes",
            workspace_key="workspace-a",
            course_id="course-a",
        )
    )
    snapshot = await wait_ready(service, ref.job_id)

    assert snapshot["status"] == "ready"
    assert service.latest("workspace-a")["job_id"] == ref.job_id
    assert snapshot["filename"] == "page.png"
    assert snapshot["target"] == "bank"
    assert snapshot["summary"] == {"total": 1, "normal": 1, "review": 0, "required": 0}
    draft = snapshot["drafts"][0]
    assert draft["question_images"] == [
        {
            "id": "source",
            "url": draft["question_images"][0]["url"],
            "filename": "page.png",
            "mime_type": "image/png",
        }
    ]
    assert attachments.resolve_path(
        session_id=f"practice-recognition-{ref.job_id}",
        attachment_id="source",
        filename="page.png",
    ).read_bytes() == b"\x89PNG\r\n\x1a\nimage-bytes"
    preview = service.stage(
        ref.job_id,
        "workspace-a",
        StageDraftsRequest(
            expected_version=snapshot["version"],
            drafts=[{**draft, "selected": True}],
        ),
    )
    assert preview["valid"] == 1
    result = PracticeStore(store.db_path).commit_import(preview["token"])
    assert result == {"created": 1, "duplicates": 0, "total": 1}
    assert service.snapshot(ref.job_id, "workspace-a")["status"] == "committed"

    listing = await store.list_notebook_entries(session_ids=[], course_id="course-a")
    assert listing["total"] == 1
    entry = listing["items"][0]
    assert entry["course_id"] == "course-a"
    assert entry["answer_origin"] == "document"
    assert entry["source_locator"] == {"page_numbers": [1]}
    assert entry["recognition_meta"]["job_id"] == ref.job_id
    assert entry["question_images"] == draft["question_images"]


@pytest.mark.asyncio
async def test_missing_answer_is_optional_and_keeps_source_image(tmp_path):
    store = SQLiteSessionStore(tmp_path / "optional-answer.db")
    attachments = LocalDiskAttachmentStore(tmp_path / "attachments")
    service = QuestionRecognitionService(
        store.db_path,
        FakeExtractor(
            [
                {
                    "question": "Explain why the sky appears blue.",
                    "correct_answer": "",
                    "answer_origin": "missing",
                }
            ]
        ),
        attachment_store=attachments,
    )
    ref = await service.start(
        StartRecognitionRequest(
            filename="question.jpg",
            mime_type="image/jpeg",
            data=b"\xff\xd8\xffimage",
            workspace_key="workspace-a",
        )
    )
    snapshot = await wait_ready(service, ref.job_id)
    draft = snapshot["drafts"][0]

    assert draft["correct_answer"] == ""
    assert draft["answer_origin"] == "missing"
    assert draft["review_level"] == "review"
    assert draft["question_images"][0]["filename"] == "question.jpg"

    preview = service.stage(
        ref.job_id,
        "workspace-a",
        StageDraftsRequest(
            expected_version=snapshot["version"],
            drafts=[{**draft, "selected": True}],
        ),
    )
    result = PracticeStore(store.db_path).commit_import(preview["token"])
    assert result == {"created": 1, "duplicates": 0, "total": 1}
    listing = await store.list_notebook_entries()
    assert listing["items"][0]["correct_answer"] == ""
    assert listing["items"][0]["answer_origin"] == "missing"
    assert listing["items"][0]["question_images"] == draft["question_images"]


@pytest.mark.asyncio
async def test_pdf_pages_are_rendered_recognized_and_saved_with_page_images(tmp_path):
    store = SQLiteSessionStore(tmp_path / "pdf-recognition.db")
    attachments = LocalDiskAttachmentStore(tmp_path / "attachments")

    class PageExtractor:
        def __init__(self):
            self.requests: list[StartRecognitionRequest] = []

        async def extract(self, request: StartRecognitionRequest) -> list[dict]:
            self.requests.append(request)
            return [
                {
                    "question": f"Question from {request.filename}",
                    "correct_answer": "",
                    "answer_origin": "missing",
                }
            ]

    extractor = PageExtractor()
    service = QuestionRecognitionService(
        store.db_path,
        extractor,
        attachment_store=attachments,
    )
    ref = await service.start(
        StartRecognitionRequest(
            filename="worksheet.pdf",
            mime_type="application/pdf",
            data=make_pdf("Question one", "Question two"),
            workspace_key="workspace-a",
        )
    )
    snapshot = await wait_ready(service, ref.job_id)

    assert snapshot["status"] == "ready"
    assert snapshot["completed_units"] == 2
    assert snapshot["total_units"] == 2
    assert len(extractor.requests) == 2
    assert [request.mime_type for request in extractor.requests] == ["image/png", "image/png"]
    assert [draft["source_locator"]["page_numbers"] for draft in snapshot["drafts"]] == [
        [1],
        [2],
    ]
    assert [draft["draft_id"] for draft in snapshot["drafts"]] == [
        "p001_q001",
        "p002_q001",
    ]
    assert [draft["question_images"][0]["id"] for draft in snapshot["drafts"]] == [
        "page-001",
        "page-002",
    ]
    for page_number in (1, 2):
        filename = f"worksheet-page-{page_number:03d}.png"
        path = attachments.resolve_path(
            session_id=f"practice-recognition-{ref.job_id}",
            attachment_id=f"page-{page_number:03d}",
            filename=filename,
        )
        assert path is not None
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    preview = service.stage(
        ref.job_id,
        "workspace-a",
        StageDraftsRequest(
            expected_version=snapshot["version"],
            drafts=[{**draft, "selected": True} for draft in snapshot["drafts"]],
        ),
    )
    assert PracticeStore(store.db_path).commit_import(preview["token"])["created"] == 2
    listing = await store.list_notebook_entries()
    assert {item["source_locator"]["page_numbers"][0] for item in listing["items"]} == {1, 2}
    assert all(item["question_images"] for item in listing["items"])


def test_pdf_renderer_rejects_page_limit_and_encryption():
    with pytest.raises(ValueError, match="at most 20 pages"):
        render_pdf_pages(make_pdf(*(f"Page {number}" for number in range(21))))

    reader = PdfReader(io.BytesIO(make_pdf("Private question")))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ValueError, match="Password-protected"):
        render_pdf_pages(encrypted.getvalue())


@pytest.mark.asyncio
async def test_suggested_answer_requires_explicit_user_confirmation(tmp_path):
    store = SQLiteSessionStore(tmp_path / "suggested.db")
    service = QuestionRecognitionService(
        store.db_path,
        FakeExtractor(
            [
                {
                    "question": "Capital of France?",
                    "correct_answer": "Paris",
                    "answer_origin": "model_suggested",
                }
            ]
        ),
        attachment_store=LocalDiskAttachmentStore(tmp_path / "attachments"),
    )
    ref = await service.start(
        StartRecognitionRequest(
            filename="page.webp",
            mime_type="image/webp",
            data=b"RIFF\x00\x00\x00\x00WEBPimage",
            workspace_key="workspace-a",
        )
    )
    snapshot = await wait_ready(service, ref.job_id)
    draft = snapshot["drafts"][0]
    assert draft["review_level"] == "required"

    with pytest.raises(ValueError, match="confirmed by the user"):
        service.stage(
            ref.job_id,
            "workspace-a",
            StageDraftsRequest(
                expected_version=snapshot["version"],
                drafts=[{**draft, "selected": True}],
            ),
        )

    preview = service.stage(
        ref.job_id,
        "workspace-a",
        StageDraftsRequest(
            expected_version=snapshot["version"],
            drafts=[{**draft, "selected": True, "answer_origin": "user"}],
        ),
    )
    assert preview["valid"] == 1


@pytest.mark.asyncio
async def test_job_scope_single_active_job_and_worker_reconciliation(tmp_path):
    store = SQLiteSessionStore(tmp_path / "scope.db")
    blocker = asyncio.Event()

    class BlockingExtractor:
        async def extract(self, request: StartRecognitionRequest) -> list[dict]:
            await blocker.wait()
            return []

    service = QuestionRecognitionService(
        store.db_path,
        BlockingExtractor(),
        attachment_store=LocalDiskAttachmentStore(tmp_path / "attachments"),
    )
    ref = await service.start(
        StartRecognitionRequest(
            filename="page.jpg",
            mime_type="image/jpeg",
            data=b"\xff\xd8\xffimage",
            workspace_key="workspace-a",
        )
    )
    await asyncio.sleep(0)
    latest = service.latest("workspace-a")
    assert latest is not None
    assert latest["job_id"] == ref.job_id
    assert latest["filename"] == "page.jpg"
    assert latest["status"] in {"queued", "validating", "recognizing"}
    with pytest.raises(ValueError, match="already running"):
        await service.start(
            StartRecognitionRequest(
                filename="other.jpg",
                mime_type="image/jpeg",
                data=b"\xff\xd8\xffother",
                workspace_key="workspace-a",
            )
        )
    with pytest.raises(LookupError):
        service.snapshot(ref.job_id, "workspace-b")

    assert service.jobs.reconcile_running("workspace-a") == 1
    assert service.snapshot(ref.job_id, "workspace-a")["error_code"] == "worker_lost"
    assert service.latest("workspace-a") is None
    blocker.set()
    await asyncio.sleep(0)


def test_recognition_schema_migration_is_idempotent(tmp_path):
    store = SQLiteSessionStore(tmp_path / "migration.db")
    SQLiteSessionStore(store.db_path)
    with PracticeStore(store.db_path).connect() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(notebook_entries)").fetchall()
        }
        assert {
            "course_id",
            "question_images_json",
            "source_locator_json",
            "answer_origin",
            "recognition_meta_json",
        } <= columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='practice_recognition_jobs'"
        ).fetchone()
