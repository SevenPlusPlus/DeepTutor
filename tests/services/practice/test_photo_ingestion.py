from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io

from PIL import Image
import pytest

from deeptutor.services.practice.photo_ingestion import PhotoIngestionService
from deeptutor.services.practice.recognition import StartRecognitionRequest
from deeptutor.services.practice.recognition.service import QuestionRecognitionService
from deeptutor.services.practice.storage import ReviewConflict
from deeptutor.services.session.sqlite_store import SQLiteSessionStore
from deeptutor.services.storage.attachment_store import LocalDiskAttachmentStore


def photo(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return output.getvalue()


class Extractor:
    def __init__(self, drafts=None):
        self.drafts = (
            drafts
            if drafts is not None
            else [
                {"question": "2 + 2 = ?", "subject": "math", "answer_origin": "missing"},
            ]
        )
        self.requests = []

    async def extract(self, request):
        self.requests.append(request)
        return self.drafts


@pytest.fixture
def setup(tmp_path):
    store = SQLiteSessionStore(tmp_path / "photos.db")
    attachments = LocalDiskAttachmentStore(tmp_path / "attachments")
    clock = [1_700_000_000.0]
    extractor = Extractor()
    service = PhotoIngestionService(
        store.db_path,
        extractor,
        attachment_store=attachments,
        clock=lambda: clock[0],
        workspace_key="workspace-a",
    )
    return service, extractor, store, attachments, clock


async def ingest(service, key="message-1", data=None, received_at=1_700_000_000.0):
    return await service.ingest(
        data=data or photo(),
        filename="page.png",
        mime_type="image/png",
        source={"event_key": key, "channel": "weixin", "partner_id": "partner-1"},
        received_at=received_at,
        source_sent_at=received_at - 3,
    )


@pytest.mark.asyncio
async def test_archives_before_recognition_and_deduplicates_receipts(setup):
    service, extractor, _, attachments, _ = setup
    first = await ingest(service)
    assert first["status"] == "queued" and not extractor.requests
    assert (
        attachments.resolve_path(
            session_id=first["asset"]["id"],
            attachment_id="original",
            filename="page.png",
        ).read_bytes()
        == photo()
    )
    repeated = await ingest(service)
    assert repeated["id"] == first["id"] and repeated["upload_count"] == 1
    assert repeated["version"] == first["version"]
    resent = await ingest(service, "message-2", received_at=first["received_at"] + 60)
    assert resent["id"] == first["id"] and resent["upload_count"] == 2
    assert resent["received_at"] == first["received_at"]
    assert resent["last_received_at"] == first["received_at"] + 60
    assert service.list()["total"] == 1

    # A failed older download may be replayed after a newer identical photo.
    earlier = await ingest(service, "message-older", received_at=first["received_at"] - 60)
    assert earlier["received_at"] == first["received_at"] - 60
    assert earlier["source_sent_at"] == first["received_at"] - 63
    assert earlier["last_received_at"] == resent["last_received_at"]
    assert earlier["upload_count"] == 3


@pytest.mark.asyncio
async def test_confirm_is_atomic_self_reported_ungraded_and_idempotent(setup):
    service, _, session_store, _, _ = setup
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    assert item["status"] == "ready" and item["subject"] == "math"
    assert item["drafts"][0]["question_images"] == [item["asset"]]
    with pytest.raises(ValueError, match="Confirm"):
        service.confirm(item["id"], expected_version=item["version"], confirmed=False)
    result = service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    assert result["status"] == "confirmed" and len(result["entry_ids"]) == 1
    assert result["result"] == {"created": 1, "duplicates": 0, "total": 1}
    duplicate = service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    assert duplicate["entry_ids"] == result["entry_ids"]
    listing = await session_store.list_notebook_entries(mistakes_only=True)
    assert listing["total"] == 1
    entry = listing["items"][0]
    assert entry["source"] == "partner_chat" and entry["result"] == "ungraded"
    assert entry["answer_origin"] == "missing"
    assert entry["recognition_meta"]["mistake_origin"] == "self_reported"
    assert entry["recognition_meta"]["first_uploaded_at"] == item["received_at"]
    with service.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM photo_question_links").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unknown_subject_and_untrusted_answer_require_review(setup):
    service, extractor, _, _, _ = setup
    extractor.drafts = [
        {"question": "Find x", "correct_answer": "4", "answer_origin": "model_suggested"}
    ]
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    assert item["status"] == "needs_subject"
    with pytest.raises(ValueError, match="subject"):
        service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    item = service.update(item["id"], expected_version=item["version"], subject="math")
    forged = [{**item["drafts"][0], "answer_origin": "document"}]
    with pytest.raises(ValueError, match="Answers absent"):
        service.confirm(item["id"], expected_version=item["version"], confirmed=True, drafts=forged)
    checked = [{**item["drafts"][0], "answer_origin": "user"}]
    result = service.confirm(
        item["id"], expected_version=item["version"], confirmed=True, drafts=checked
    )
    assert result["status"] == "confirmed"


@pytest.mark.asyncio
async def test_changed_document_answer_cannot_be_forged_as_document(setup):
    service, extractor, _, _, _ = setup
    extractor.drafts = [
        {
            "question": "Find x",
            "subject": "math",
            "correct_answer": "4",
            "answer_origin": "document",
        }
    ]
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    edited = [{**item["drafts"][0], "correct_answer": "5"}]
    with pytest.raises(ValueError, match="Answers absent"):
        service.confirm(item["id"], expected_version=item["version"], confirmed=True, drafts=edited)


@pytest.mark.asyncio
async def test_continuous_images_are_queued_and_scope_isolated(setup):
    service, _, store, attachments, _ = setup
    for index in range(10):
        await ingest(service, f"message-{index}", photo((index, 0, 0)))
    assert service.list()["total"] == 10
    assert await service.process_pending(limit=10) == 10
    assert {item["status"] for item in service.list()["items"]} == {"ready"}
    other = PhotoIngestionService(
        store.db_path,
        Extractor(),
        attachment_store=attachments,
        workspace_key="workspace-b",
    )
    item_id = service.list()["items"][0]["id"]
    assert other.list()["total"] == 0
    with pytest.raises(LookupError):
        other.detail(item_id)
    with pytest.raises(LookupError):
        other.update(item_id, expected_version=1, subject="english")
    assert await other.process_pending() == 0


@pytest.mark.asyncio
async def test_restart_reclaims_expired_lease_and_old_worker_cannot_overwrite(setup):
    service, extractor, store, attachments, clock = setup
    item = await ingest(service)
    old_claim = service._claim()
    replacement = PhotoIngestionService(
        store.db_path,
        extractor,
        attachment_store=attachments,
        workspace_key="workspace-a",
        clock=lambda: clock[0],
    )
    assert await replacement.process_pending() == 0
    clock[0] += 241
    assert await replacement.process_pending() == 1
    current = replacement.detail(item["id"])
    assert current["status"] == "ready"
    with service.store.connect() as conn:
        result = conn.execute(
            "UPDATE photo_inbox_items SET status='failed' WHERE id=? AND lease_token=?",
            (item["id"], old_claim["lease_token"]),
        )
        assert result.rowcount == 0


@pytest.mark.asyncio
async def test_failure_backoff_then_manual_retry_and_supplement_keep_original(setup):
    service, extractor, _, attachments, clock = setup

    async def fail(_request):
        raise RuntimeError("secret-provider-token")

    extractor.extract = fail
    item = await ingest(service)
    for _ in range(3):
        assert await service.process_pending() == 1
        clock[0] += 100
    failed = service.detail(item["id"])
    assert failed["status"] == "failed" and "secret" not in failed["error"]
    assert failed["asset"] == item["asset"]
    supplemented = await service.supplement(
        item["id"],
        expected_version=failed["version"],
        data=photo("black"),
        filename="clear.png",
        mime_type="image/png",
    )
    assert supplemented["status"] == "queued"
    assert supplemented["received_at"] == item["received_at"]
    assert supplemented["asset"] == item["asset"] and len(supplemented["assets"]) == 2
    assert supplemented["upload_count"] == 1
    assert (
        attachments.resolve_path(
            session_id=item["asset"]["id"],
            attachment_id="original",
            filename="page.png",
        ).read_bytes()
        == photo()
    )


@pytest.mark.asyncio
async def test_non_question_and_conflicting_revisions(setup):
    service, extractor, _, _, _ = setup
    extractor.drafts = []
    item = await ingest(service)
    await service.process_pending()
    current = service.detail(item["id"])
    assert current["status"] == "non_question"
    assert service.list(status="pending")["total"] == 1
    with pytest.raises(ReviewConflict):
        service.retry(item["id"], expected_version=item["version"])
    retried = service.retry(item["id"], expected_version=current["version"])
    assert retried["received_at"] == item["received_at"]


@pytest.mark.asyncio
async def test_two_questions_share_original_and_filter_received_time(setup):
    service, extractor, _, _, _ = setup
    extractor.drafts = [
        {"question": "Find x", "subject": "math"},
        {"question": "Find y", "subject": "math"},
    ]
    item = await ingest(service)
    await service.process_pending()
    current = service.detail(item["id"])
    assert len(current["drafts"]) == 2
    assert current["drafts"][0]["question_images"] == current["drafts"][1]["question_images"]
    assert service.list(since=item["received_at"], until=item["received_at"] + 1)["total"] == 1
    assert service.list(since=item["received_at"] + 1)["total"] == 0
    assert service.list(subject="math", q="Find x")["total"] == 1
    assert service.list(q="%")["total"] == 0

    drafts = [
        {**current["drafts"][0], "subject": "math"},
        {**current["drafts"][1], "subject": "physics"},
    ]
    current = service.update(item["id"], expected_version=current["version"], drafts=drafts)
    assert current["subject"] == "unclassified" and current["status"] == "ready"
    assert service.list(subject="math")["total"] == 1
    assert service.list(subject="physics")["total"] == 1
    assert service.list(subject="unclassified")["total"] == 0


@pytest.mark.asyncio
async def test_photo_lease_blocks_existing_recognition_importer(setup):
    service, extractor, store, attachments, clock = setup
    await ingest(service)
    assert service._claim()
    importer = QuestionRecognitionService(
        store.db_path,
        extractor,
        attachment_store=attachments,
        clock=lambda: clock[0],
    )
    with pytest.raises(ValueError, match="photo recognition"):
        await importer.start(
            StartRecognitionRequest(
                filename="image.png",
                mime_type="image/png",
                data=photo(),
                workspace_key="workspace-a",
            )
        )


@pytest.mark.asyncio
async def test_cancelled_worker_keeps_lease_for_recovery(setup):
    service, extractor, _, _, clock = setup
    entered = asyncio.Event()

    async def block(_request):
        entered.set()
        await asyncio.Event().wait()

    extractor.extract = block
    item = await ingest(service)
    task = asyncio.create_task(service.process_pending())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.detail(item["id"])["status"] == "recognizing"
    assert service._claim() is None
    clock[0] += 241
    assert service._claim()["id"] == item["id"]


@pytest.mark.asyncio
async def test_different_photo_same_question_links_without_resetting_review(setup):
    service, _, _, _, _ = setup
    first = await ingest(service)
    await service.process_pending()
    first = service.detail(first["id"])
    committed = service.confirm(first["id"], expected_version=first["version"], confirmed=True)
    entry_id = committed["entry_ids"][0]
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET is_mistake=0,review_count=4,interval_days=14,due_at=1234567890 WHERE entry_id=?",
            (entry_id,),
        )
    second = await ingest(service, "another-image", photo("black"))
    await service.process_pending()
    second = service.detail(second["id"])
    duplicate = service.confirm(second["id"], expected_version=second["version"], confirmed=True)
    assert duplicate["entry_ids"] == [entry_id]
    assert duplicate["result"] == {"created": 0, "duplicates": 1, "total": 1}
    state = service.store.state(entry_id)
    assert state["is_mistake"] == 0 and state["review_count"] == 4
    assert state["interval_days"] == 14 and state["due_at"] == 1234567890
    with service.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM photo_question_links").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_storage_failure_never_creates_archived_receipt(setup):
    service, _, _, _, _ = setup

    async def fail(**kwargs):
        raise OSError("disk full")

    service.attachments.put = fail
    with pytest.raises(OSError, match="disk full"):
        await ingest(service)
    assert service.list()["total"] == 0


@pytest.mark.asyncio
async def test_soft_remove_pauses_work_and_restore_preserves_original_and_entries(setup):
    service, _, store, attachments, clock = setup
    item = await ingest(service)
    removed = service.set_archived(item["id"], expected_version=item["version"], archived=True)
    assert removed["archived_at"] == clock[0]
    assert service.list()["total"] == 0 and service.list(status="removed")["total"] == 1
    assert await service.process_pending() == 0
    with pytest.raises(ReviewConflict, match="Restore"):
        service.confirm(item["id"], expected_version=removed["version"], confirmed=True)
    restored = service.set_archived(item["id"], expected_version=removed["version"], archived=False)
    assert restored["asset"] == item["asset"] and restored["received_at"] == item["received_at"]
    await service.process_pending()
    restored = service.detail(item["id"])
    committed = service.confirm(item["id"], expected_version=restored["version"], confirmed=True)
    service.set_archived(item["id"], expected_version=committed["version"], archived=True)
    assert (await store.list_notebook_entries(mistakes_only=True))["total"] == 1
    assert attachments.resolve_path(
        session_id=item["asset"]["id"],
        attachment_id="original",
        filename="page.png",
    ).is_file()


@pytest.mark.asyncio
async def test_removal_while_recognizing_never_reappears_on_completion(setup):
    service, extractor, _, _, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(_request):
        entered.set()
        await release.wait()
        return [{"question": "Find x", "subject": "math"}]

    extractor.extract = block
    item = await ingest(service)
    task = asyncio.create_task(service.process_pending())
    await entered.wait()
    running = service.detail(item["id"])
    service.set_archived(item["id"], expected_version=running["version"], archived=True)
    release.set()
    await task
    assert service.list()["total"] == 0
    removed = service.list(status="removed")["items"][0]
    assert removed["archived_at"] is not None and removed["entry_ids"] == []
    assert removed["status"] == "ready"


@pytest.mark.asyncio
async def test_old_duplicate_delivery_does_not_restore_but_new_upload_does(setup):
    service, _, _, _, clock = setup
    item = await ingest(service)
    service.set_archived(item["id"], expected_version=item["version"], archived=True)
    assert (await ingest(service))["archived_at"] is not None
    assert (await ingest(service, "delayed-old", received_at=clock[0] - 10))[
        "archived_at"
    ] is not None
    resent = await ingest(service, "new-upload", received_at=clock[0] + 10)
    assert resent["archived_at"] is None
    assert service.list()["total"] == 1


@pytest.mark.asyncio
async def test_tampered_image_and_empty_content_rejected(setup):
    service, _, _, _, _ = setup
    with pytest.raises(ValueError, match="damaged"):
        await ingest(service, data=b"\x89PNG\r\n\x1a\ninvalid")
    with pytest.raises(ValueError, match="declared type"):
        await service.ingest(
            data=photo(),
            filename="wrong.jpg",
            mime_type="image/jpeg",
            source={"event_key": "new"},
            received_at=1_700_000_000,
        )
    assert service.list()["total"] == 0


@pytest.mark.asyncio
async def test_confirm_direct_subject_edits_update_archive_classification(setup):
    service, _, _, _, _ = setup
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    drafts = [{**item["drafts"][0], "subject": "physics"}]
    confirmed = service.confirm(
        item["id"],
        expected_version=item["version"],
        confirmed=True,
        drafts=drafts,
    )
    assert confirmed["subject"] == "physics"
    assert service.list(subject="physics")["total"] == 1
    assert service.list(subject="math")["total"] == 0


@pytest.mark.asyncio
async def test_confirm_aggregates_unselected_draft_subjects_like_update(setup):
    service, extractor, _, _, _ = setup
    extractor.drafts = [
        {"question": "Find x", "subject": "math"},
        {"question": "Find y", "subject": "math"},
    ]
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    drafts = [
        {**item["drafts"][0], "subject": "physics"},
        {**item["drafts"][1], "selected": False},
    ]
    confirmed = service.confirm(
        item["id"],
        expected_version=item["version"],
        confirmed=True,
        drafts=drafts,
    )
    assert confirmed["subject"] == "unclassified"
    assert len(confirmed["entry_ids"]) == 1
    assert service.list(subject="physics")["total"] == 1
    assert service.list(subject="math")["total"] == 1


def test_additive_schema_migration_is_safe_for_concurrent_constructors(setup):
    service, extractor, store, attachments, _ = setup
    with service.store.connect() as conn:
        conn.execute("ALTER TABLE photo_inbox_items DROP COLUMN archived_at")

    def create(_):
        return PhotoIngestionService(
            store.db_path,
            extractor,
            attachment_store=attachments,
            workspace_key="workspace-a",
        ).list()["total"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(create, range(8))) == [0] * 8
    with service.store.connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(photo_inbox_items)")]
        assert columns.count("archived_at") == 1


@pytest.mark.asyncio
async def test_deleted_confirmed_entry_reopens_archive_only_until_user_confirms(setup):
    service, _, store, _, _ = setup
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    confirmed = service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    deleted_id = confirmed["entry_ids"][0]
    assert await store.delete_notebook_entry(deleted_id)
    reopened = service.detail(item["id"])
    assert reopened["status"] == "ready" and reopened["entry_ids"] == []
    assert reopened["version"] > confirmed["version"]
    assert service.list()["counts"]["confirmed"] == 0
    assert await service.process_pending() == 0
    assert (await store.list_notebook_entries())["total"] == 0
    with pytest.raises(ReviewConflict):
        service.confirm(item["id"], expected_version=confirmed["version"], confirmed=True)
    restored = service.confirm(item["id"], expected_version=reopened["version"], confirmed=True)
    assert restored["status"] == "confirmed"
    assert restored["entry_ids"] != [deleted_id]
    assert restored["result"] == {"created": 1, "duplicates": 0, "total": 1}


@pytest.mark.asyncio
async def test_partially_deleted_photo_reconfirmation_preserves_remaining_review(setup):
    service, extractor, store, _, _ = setup
    extractor.drafts = [
        {"question": "Find x", "subject": "math"},
        {"question": "Find y", "subject": "math"},
    ]
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    confirmed = service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    deleted_id, retained_id = confirmed["entry_ids"]
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET review_count=4,interval_days=14,is_mistake=0 WHERE entry_id=?",
            (retained_id,),
        )
    assert await store.delete_notebook_entry(deleted_id)
    listing = service.list(status="ready")
    reopened = listing["items"][0]
    assert listing["counts"]["confirmed"] == 0
    assert reopened["entry_ids"] == [retained_id]
    restored = service.confirm(item["id"], expected_version=reopened["version"], confirmed=True)
    assert restored["result"] == {"created": 1, "duplicates": 1, "total": 2}
    assert len(restored["entry_ids"]) == 2 and retained_id in restored["entry_ids"]
    retained = service.store.state(retained_id)
    assert retained["review_count"] == 4 and retained["interval_days"] == 14
    assert retained["is_mistake"] == 0
    assert (await store.list_notebook_entries())["total"] == 2


@pytest.mark.asyncio
async def test_confirm_transaction_rechecks_deletion_after_detail_snapshot(setup, monkeypatch):
    service, _, _, _, _ = setup
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    confirmed = service.confirm(item["id"], expected_version=item["version"], confirmed=True)
    original_commit = service.store.commit_import

    def delete_before_commit(token):
        with service.store.connect() as conn:
            conn.execute("DELETE FROM notebook_entries WHERE id=?", (confirmed["entry_ids"][0],))
        return original_commit(token)

    monkeypatch.setattr(service.store, "commit_import", delete_before_commit)
    with pytest.raises(ReviewConflict):
        service.confirm(item["id"], expected_version=confirmed["version"], confirmed=True)
    assert service.detail(item["id"])["entry_ids"] == []
    with service.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notebook_entries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_confirm_preserves_review_metadata_and_records_answer_exposure(setup):
    service, extractor, session_store, _, _ = setup
    extractor.drafts = [
        {
            "question": "2 + 2 = ?",
            "question_type": "fill_blank",
            "correct_answer": "4",
            "answer_origin": "document",
            "subject": "math",
        }
    ]
    item = await ingest(service)
    await service.process_pending()
    item = service.detail(item["id"])
    drafts = [
        {
            **item["drafts"][0],
            "grade": "五年级",
            "knowledge_point": "整数加法",
            "tags": ["计算失误", "期中重点"],
        }
    ]
    result = service.confirm(
        item["id"], expected_version=item["version"], confirmed=True, drafts=drafts
    )
    entry_id = result["entry_ids"][0]
    entry = await session_store.get_notebook_entry(entry_id)
    assert entry["recognition_meta"]["grade"] == "五年级"
    assert entry["recognition_meta"]["photo_source_channel"] == "weixin"
    assert entry["recognition_meta"]["photo_partner_id"] == "partner-1"
    assert entry["knowledge_point_id"] == "整数加法"
    assert {tag["name"] for tag in entry["categories"]} == {"计算失误", "期中重点"}
    assert (
        await session_store.list_notebook_entries(
            mistakes_only=True, subject="math", grade="五年级", search="整数 计算失误"
        )
    )["total"] == 1
    state = service.store.state(entry_id)
    outcome = service.store.review(
        entry_id, "after_photo_confirmation", state["version"], "good", "4", auto_grade=True
    )
    assert outcome["correct"] is True
    assert not outcome["independent"]
