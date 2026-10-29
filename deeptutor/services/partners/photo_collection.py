"""Resolve private photo ownership without inheriting a Partner's shared scope."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from deeptutor.multi_user.paths import user_context
from deeptutor.services.partners.interaction import actor_for_account
from deeptutor.services.partners.links import linked_user_id
from deeptutor.services.workspace import get_content_workspace_service
from deeptutor.services.workspace.activity import data_activity
from deeptutor.services.workspace.context import current_workspace_id, workspace_context


def _image_items(message: dict) -> list[tuple[int, dict]]:
    images = [
        (index, item.get("image_item") or {})
        for index, item in enumerate(message.get("item_list") or [])
        if item.get("type") == 2
    ]
    if images:
        return images
    for index, item in enumerate(message.get("item_list") or []):
        ref = (item.get("ref_msg") or {}).get("message_item") or {}
        if ref.get("type") == 2:
            return [(index, ref.get("image_item") or {})]
    return []


def freeze_photo_target(partner_id: str, config, message: dict) -> dict:
    """Called once on first durable receipt, before downloads or cursor advance."""
    if message.get("message_type") == 2 or message.get("is_group") or not _image_items(message):
        return {"collect": False}
    texts = [
        str((item.get("text_item") or {}).get("text") or "")
        for item in message.get("item_list") or []
        if item.get("type") == 1
    ]
    explicit = any(re.search(r"(?:^|\s)/错题(?:\s|$)|加入错题本", text) for text in texts)
    if not config.photo_collection_enabled and not explicit:
        return {"collect": False}
    user_id = linked_user_id(partner_id, "weixin", str(message.get("from_user_id") or ""))
    if not user_id:
        return {"collect": True, "error": "unlinked"}
    actor = actor_for_account(user_id)
    if actor is None:
        return {"collect": True, "error": "account_unavailable"}
    try:
        with user_context(actor), workspace_context(config.photo_workspace_id):
            workspace_id = current_workspace_id()
            if workspace_id:
                get_content_workspace_service().validate_chat_binding(workspace_id)
            return {"collect": True, "user_id": user_id, "workspace_id": workspace_id}
    except (ValueError, RuntimeError):
        return {"collect": True, "error": "workspace_unavailable"}


@contextmanager
def photo_target_context(target: dict):
    """Revalidate the frozen target; never look up a newer channel link."""
    actor = actor_for_account(str(target.get("user_id") or ""))
    if actor is None:
        raise ValueError("Photo owner is unavailable")
    workspace_id = str(target.get("workspace_id") or "")
    # Resolve and retain the workspace under a shared maintenance lease. The
    # detached channel writer crosses awaits while saving its original image;
    # moving the workspace in that interval could otherwise split asset and DB.
    with user_context(actor), data_activity(), workspace_context(workspace_id):
        if workspace_id:
            get_content_workspace_service().validate_chat_binding(workspace_id)
        yield


def photo_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported photo format")


async def ingest_photo(
    *,
    target: dict,
    path: str,
    source: dict,
    received_at: float,
    source_sent_at: float | None,
) -> dict:
    from deeptutor.services.practice.photo_ingestion import get_photo_ingestion_service
    from deeptutor.services.practice.recognition.service import MAX_SOURCE_BYTES

    with photo_target_context(target):
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        return await get_photo_ingestion_service().ingest(
            data=data,
            filename=Path(path).name,
            mime_type=photo_mime(data),
            source=source,
            received_at=received_at,
            source_sent_at=source_sent_at,
        )


def list_photo_scopes(partner_id: str, state_dir: str = "") -> list[dict]:
    """Scopes with durable photos, for startup recognition recovery."""
    from deeptutor.partners.channels.weixin_inbox import WeixinInbox
    from deeptutor.partners.config.paths import get_partner_channel_dir

    directory = (
        Path(state_dir).expanduser() if state_dir else get_partner_channel_dir(partner_id, "weixin")
    )
    if not (directory / "photo-inbox.sqlite3").is_file():
        return []
    return WeixinInbox(directory).photo_scopes()
