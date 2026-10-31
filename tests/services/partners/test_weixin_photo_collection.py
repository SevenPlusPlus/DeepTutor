"""Receipt persistence, frozen ownership and acknowledgement recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from deeptutor.modules import catalog as module_catalog
from deeptutor.partners.bus.queue import MessageBus
from deeptutor.partners.channels.weixin import WeixinChannel, WeixinConfig
from deeptutor.partners.channels.weixin_inbox import WeixinInbox
from deeptutor.services.partners import photo_collection


def photo_message(message_id="photo-1", **overrides):
    return {
        "message_id": message_id,
        "from_user_id": "wx-alice",
        "create_time_ms": 1700000000000,
        "context_token": "private-context",
        "item_list": [
            {"type": 2, "image_item": {"media": {"full_url": "https://cdn.example/private"}}}
        ],
        **overrides,
    }


def channel(tmp_path):
    bus = MagicMock(spec=MessageBus)
    bus.publish_inbound = AsyncMock()
    result = WeixinChannel(
        WeixinConfig(
            enabled=True,
            token="secret",
            allow_from=["*"],
            state_dir=str(tmp_path),
            photo_collection_enabled=True,
            bot_id="bot",
        ),
        bus,
    )
    result.partner_id = "math-tutor"
    result.send = AsyncMock()
    return result


def frozen(_message):
    return {"collect": True, "user_id": "alice", "workspace_id": "algebra"}


def make_retry_due(inbox):
    with sqlite3.connect(inbox.path) as conn:
        conn.execute("UPDATE channel_inbox SET next_retry_at=0")
        conn.execute("UPDATE photo_outbox SET next_retry_at=0")


@pytest.fixture(params=["disabled", "not_installed"])
def questions_availability(request, monkeypatch):
    state = {"enabled": False}
    installed = module_catalog.installed
    monkeypatch.setattr(
        module_catalog,
        "selection",
        lambda: {"questions": state["enabled"]} if request.param == "disabled" else {},
    )
    monkeypatch.setattr(
        module_catalog,
        "installed",
        lambda name: (
            state["enabled"]
            if name == "questions" and request.param == "not_installed"
            else installed(name)
        ),
    )
    return state


@pytest.mark.asyncio
async def test_unavailable_questions_keeps_new_photos_in_normal_chat(
    tmp_path, monkeypatch, questions_availability
):
    ch = channel(tmp_path)
    ch._process_message = AsyncMock()
    ch._download_media_item = AsyncMock()
    linked_user = MagicMock(side_effect=AssertionError("Photo ownership must not be resolved"))
    monkeypatch.setattr(photo_collection, "linked_user_id", linked_user)
    messages = [
        photo_message("automatic"),
        photo_message(
            "explicit",
            item_list=[
                {"type": 1, "text_item": {"text": "/错题"}},
                *photo_message()["item_list"],
            ],
        ),
    ]
    inbox = ch._durable_inbox()
    inbox.capture_batch(
        messages,
        partner_id=ch.partner_id,
        account_id="bot",
        freeze_target=lambda message: photo_collection.freeze_photo_target(
            ch.partner_id, ch.config, message
        ),
    )
    assert all(not event["target"]["collect"] for event in inbox.pending())

    await ch._drain_inbox()

    assert ch._process_message.await_count == 2
    ch._download_media_item.assert_not_awaited()
    linked_user.assert_not_called()
    ch.send.assert_not_awaited()
    assert inbox.pending() == []


@pytest.mark.asyncio
async def test_frozen_photo_waits_for_questions_without_consuming_retries(
    tmp_path, monkeypatch, questions_availability
):
    ch = channel(tmp_path)
    ch._download_media_item = AsyncMock(return_value="/private/photo.jpg")
    ingested = AsyncMock(return_value={"id": "saved-photo"})
    monkeypatch.setattr(photo_collection, "ingest_photo", ingested)
    inbox = ch._durable_inbox()
    inbox.capture_batch(
        [photo_message()],
        partner_id=ch.partner_id,
        account_id="bot",
        freeze_target=frozen,
        received_at=42,
    )
    original = inbox.pending()[0]

    for _ in range(10):
        make_retry_due(inbox)
        await ch._drain_inbox()

    pending = inbox.pending()[0]
    assert pending["target"] == original["target"]
    assert pending["payload"] == original["payload"]
    assert pending["received_at"] == 42
    assert pending["attempts"] == 0
    ch._download_media_item.assert_not_awaited()
    ingested.assert_not_awaited()
    ch.send.assert_not_awaited()

    questions_availability["enabled"] = True
    restarted = channel(tmp_path)
    restarted.config.photo_collection_enabled = False
    restarted.config.photo_workspace_id = "new-workspace"
    restarted._download_media_item = AsyncMock(return_value="/private/photo.jpg")
    await restarted._drain_inbox()

    assert ingested.call_args.kwargs["target"] == original["target"]
    assert ingested.call_args.kwargs["received_at"] == 42
    assert inbox.pending() == []
    restarted.send.assert_awaited_once()
    assert "已保存1张" in restarted.send.call_args[0][0].content


@pytest.mark.asyncio
async def test_unavailable_questions_rejects_direct_ingest_before_ownership_or_file_access(
    monkeypatch, questions_availability
):
    owner_context = MagicMock(side_effect=AssertionError("Must not enter a photo owner's scope"))
    monkeypatch.setattr(photo_collection, "photo_target_context", owner_context)

    with pytest.raises(module_catalog.ModuleUnavailable, match="questions"):
        await photo_collection.ingest_photo(
            target=frozen(None),
            path="/nonexistent/photo.jpg",
            source={"event_key": "one"},
            received_at=1,
            source_sent_at=None,
        )
    owner_context.assert_not_called()


def test_whole_batch_is_atomic_and_first_target_time_is_immutable(tmp_path):
    inbox = WeixinInbox(tmp_path)
    messages = [photo_message("one"), photo_message("two")]

    def interrupted(message):
        if message["message_id"] == "two":
            raise RuntimeError("simulated interrupted admission")
        return frozen(message)

    with pytest.raises(RuntimeError):
        inbox.capture_batch(
            messages, partner_id="p", account_id="wx-bot", freeze_target=interrupted
        )
    assert inbox.pending() == []
    inbox.capture_batch(
        messages, partner_id="p", account_id="wx-bot", freeze_target=frozen, received_at=100
    )
    inbox.capture_batch(
        messages,
        partner_id="p",
        account_id="wx-bot",
        freeze_target=lambda _: {"user_id": "bob"},
        received_at=200,
    )
    assert len(inbox.pending()) == 2
    assert all(
        row["target"]["user_id"] == "alice" and row["received_at"] == 100 for row in inbox.pending()
    )
    assert inbox.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_cursor_is_not_advanced_when_batch_cannot_be_persisted(tmp_path, monkeypatch):
    ch = channel(tmp_path)
    ch.config.bot_id = "original-bot"
    ch._client = httpx.AsyncClient()
    ch._get_updates_buf = "previous"
    ch._api_post = AsyncMock(return_value={"get_updates_buf": "next", "msgs": [photo_message()]})
    monkeypatch.setattr(
        WeixinInbox,
        "capture_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    try:
        with pytest.raises(OSError):
            await ch._poll_once()
        assert ch._get_updates_buf == "previous"
    finally:
        await ch._client.aclose()


@pytest.mark.asyncio
async def test_original_is_saved_before_ack_and_ack_retry_does_not_reingest(tmp_path, monkeypatch):
    ch = channel(tmp_path)
    ch.config.bot_id = "original-bot"
    ch._download_media_item = AsyncMock(return_value="/private/photo.jpg")
    ingested = AsyncMock(return_value={"id": "receipt-1"})
    monkeypatch.setattr(photo_collection, "ingest_photo", ingested)
    ch._durable_inbox().capture_batch(
        [photo_message()],
        partner_id=ch.partner_id,
        account_id="original-bot",
        freeze_target=frozen,
        received_at=123,
    )

    async def failed_ack(_msg):
        assert ingested.await_count == 1
        raise OSError("send unavailable")

    ch.send.side_effect = failed_ack
    await ch._drain_inbox()
    call = ingested.call_args.kwargs
    assert call["target"]["user_id"] == "alice"
    assert call["received_at"] == 123
    assert call["source_sent_at"] == 1700000000
    assert call["source"]["channel_account_id"] == "original-bot"
    assert call["source"]["media_index"] == 0
    assert ch._durable_inbox().pending() == []
    ch.bus.publish_inbound.assert_not_awaited()

    # Restart with the same channel account: only the persisted acknowledgement
    # is retried; no second ingestion is needed.
    restarted = channel(tmp_path)
    restarted.config.bot_id = "original-bot"
    make_retry_due(restarted._durable_inbox())
    await restarted._drain_inbox()
    assert ingested.await_count == 1
    restarted.send.assert_awaited_once()
    assert "已保存1张" in restarted.send.call_args[0][0].content
    with sqlite3.connect(restarted._durable_inbox().path) as conn:
        assert json.loads(conn.execute("SELECT payload FROM channel_inbox").fetchone()[0]) == {}
        assert conn.execute("SELECT delivered FROM photo_outbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_failed_download_survives_restart_and_preserves_first_receipt(tmp_path, monkeypatch):
    ch = channel(tmp_path)
    ch._download_media_item = AsyncMock(return_value=None)
    ingested = AsyncMock()
    monkeypatch.setattr(photo_collection, "ingest_photo", ingested)
    ch._durable_inbox().capture_batch(
        [photo_message()],
        partner_id=ch.partner_id,
        account_id="bot",
        freeze_target=frozen,
        received_at=42,
    )
    await ch._drain_inbox()
    ingested.assert_not_awaited()
    ch.send.assert_not_awaited()
    restarted = channel(tmp_path)
    restarted._download_media_item = AsyncMock(return_value="/private/photo.jpg")
    make_retry_due(restarted._durable_inbox())
    await restarted._drain_inbox()
    assert ingested.call_args.kwargs["received_at"] == 42
    assert ingested.call_args.kwargs["target"]["user_id"] == "alice"
    restarted.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failure_notifies_once_and_failed_notification_survives_restart(
    tmp_path, monkeypatch
):
    ch = channel(tmp_path)
    ch._download_media_item = AsyncMock(return_value="/private/photo.jpg")
    ingestion = AsyncMock(side_effect=sqlite3.OperationalError("database unavailable"))
    monkeypatch.setattr(photo_collection, "ingest_photo", ingestion)
    inbox = ch._durable_inbox()
    inbox.capture_batch(
        [photo_message()], partner_id=ch.partner_id, account_id="bot", freeze_target=frozen
    )
    ch.send.side_effect = OSError("notification unavailable")
    for attempt in range(8):
        make_retry_due(inbox)
        await ch._drain_inbox()
        if attempt < 7:
            ch.send.assert_not_awaited()
    assert ingestion.await_count == 8
    ch.send.assert_awaited_once()
    assert "未完成全部归档" in ch.send.call_args[0][0].content
    with sqlite3.connect(inbox.path) as conn:
        assert conn.execute("SELECT status FROM channel_inbox").fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM photo_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT delivered FROM photo_outbox").fetchone()[0] == 0

    restarted = channel(tmp_path)
    make_retry_due(restarted._durable_inbox())
    await restarted._drain_inbox()
    assert ingestion.await_count == 8
    restarted.send.assert_awaited_once()
    with sqlite3.connect(inbox.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM photo_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT delivered FROM photo_outbox").fetchone()[0] == 1


def test_opt_in_mode_explicit_command_and_unlinked_sender(monkeypatch):
    monkeypatch.setattr(photo_collection, "linked_user_id", lambda *_: None)
    cfg = WeixinConfig()
    assert photo_collection.freeze_photo_target("p", cfg, photo_message()) == {"collect": False}
    explicit = photo_message(
        item_list=[
            {"type": 1, "text_item": {"text": "/错题"}},
            *photo_message()["item_list"],
        ]
    )
    assert photo_collection.freeze_photo_target("p", cfg, explicit) == {
        "collect": True,
        "error": "unlinked",
    }
    cfg.photo_collection_enabled = True
    assert photo_collection.freeze_photo_target("p", cfg, photo_message())["error"] == "unlinked"
    assert photo_collection.freeze_photo_target("p", cfg, photo_message(is_group=True)) == {
        "collect": False
    }


@pytest.mark.asyncio
async def test_another_qr_account_does_not_send_old_photo_ack(tmp_path):
    ch = channel(tmp_path)
    inbox = ch._durable_inbox()
    inbox.capture_batch(
        [photo_message()], partner_id=ch.partner_id, account_id="old-bot", freeze_target=frozen
    )
    event = inbox.pending()[0]
    inbox.complete(event["event_key"], chat_id="wx-alice", reply="原图已保存")
    await ch._drain_inbox()
    ch.send.assert_not_awaited()
    assert len(inbox.notifications("old-bot")) == 1
    ch.config.bot_id = "old-bot"
    await ch._drain_inbox()
    ch.send.assert_awaited_once()


def test_known_qr_account_change_clears_old_cursor_and_context(tmp_path):
    original = channel(tmp_path)
    original.config.bot_id = "old-bot"
    original._get_updates_buf = "old-cursor"
    original._context_tokens = {"wx-alice": "old-private-context"}
    original._save_state()
    replacement = channel(tmp_path)
    replacement.config.bot_id = "new-bot"
    assert replacement._load_state()
    assert replacement._get_updates_buf == ""
    assert replacement._context_tokens == {}


def test_private_target_does_not_follow_relinked_chat_account(partners_root):
    from deeptutor.multi_user import identity
    from deeptutor.multi_user.context import get_current_user
    from deeptutor.services.partners import links

    identity.save_user("root", "hash", "admin")
    alice = identity.save_user("alice", "hash", "user")
    bob = identity.save_user("bob", "hash", "user")
    for user in (alice, bob):
        code = links.issue_link_code("math-tutor", user["id"]).code
        links.redeem_link_code("math-tutor", code, channel="weixin", sender_id="wx-alice")
        if user is alice:
            target = photo_collection.freeze_photo_target(
                "math-tutor", WeixinConfig(photo_collection_enabled=True), photo_message()
            )
    assert target["user_id"] == alice["id"]
    assert links.linked_user_id("math-tutor", "weixin", "wx-alice") == bob["id"]
    with photo_collection.photo_target_context(target):
        assert get_current_user().id == alice["id"]


@pytest.mark.asyncio
async def test_detached_ingest_holds_original_owners_maintenance_lease(
    partners_root, tmp_path, monkeypatch
):
    from deeptutor.multi_user import identity
    from deeptutor.multi_user.context import get_current_user
    from deeptutor.multi_user.paths import user_context
    from deeptutor.services.partners import links
    from deeptutor.services.partners.interaction import actor_for_account
    from deeptutor.services.practice import photo_ingestion
    from deeptutor.services.workspace import WorkspaceError
    from deeptutor.services.workspace.activity import data_activity

    identity.save_user("root", "hash", "admin")
    alice = identity.save_user("alice", "hash", "user")
    bob = identity.save_user("bob", "hash", "user")
    code = links.issue_link_code("math-tutor", alice["id"]).code
    links.redeem_link_code("math-tutor", code, channel="weixin", sender_id="wx-alice")
    target = photo_collection.freeze_photo_target(
        "math-tutor", WeixinConfig(photo_collection_enabled=True), photo_message()
    )
    # A newer link must not change either the photo owner or the activity lock.
    code = links.issue_link_code("math-tutor", bob["id"]).code
    links.redeem_link_code("math-tutor", code, channel="weixin", sender_id="wx-alice")
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    entered = []

    async def ingest(**_kwargs):
        entered.append(get_current_user().id)
        await asyncio.sleep(0)
        assert get_current_user().id == alice["id"]
        with pytest.raises(WorkspaceError, match="busy"):
            with data_activity(exclusive=True):
                pass
        return {"id": "saved-photo"}

    monkeypatch.setattr(
        photo_ingestion, "get_photo_ingestion_service", lambda: SimpleNamespace(ingest=ingest)
    )
    kwargs = dict(
        target=target,
        path=str(source),
        source={"event_key": "one"},
        received_at=1,
        source_sent_at=None,
    )
    with user_context(actor_for_account(alice["id"])), data_activity(exclusive=True):
        with pytest.raises(WorkspaceError, match="busy"):
            await photo_collection.ingest_photo(**kwargs)
    assert entered == []
    assert await photo_collection.ingest_photo(**kwargs) == {"id": "saved-photo"}
    assert entered == [alice["id"]]
