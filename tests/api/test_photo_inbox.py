from __future__ import annotations

import asyncio
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from deeptutor.api.routers import photo_inbox
from deeptutor.services.practice.photo_ingestion import PhotoIngestionService
from deeptutor.services.session.sqlite_store import SQLiteSessionStore
from deeptutor.services.storage.attachment_store import LocalDiskAttachmentStore


class Extractor:
    async def extract(self, request):
        return [{"question": "Find x", "subject": "math", "answer_origin": "missing"}]


@pytest.fixture
def api(tmp_path, monkeypatch):
    db = SQLiteSessionStore(tmp_path / "photo-api.db")
    service = PhotoIngestionService(
        db.db_path,
        Extractor(),
        attachment_store=LocalDiskAttachmentStore(tmp_path / "assets"),
    )
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    item = asyncio.run(
        service.ingest(
            data=image.getvalue(),
            filename="page.png",
            mime_type="image/png",
            source={"event_key": "wechat-1", "channel": "weixin"},
            received_at=1_700_000_000,
        )
    )
    monkeypatch.setattr(photo_inbox, "get_photo_ingestion_service", lambda: service)
    app = FastAPI()
    app.include_router(photo_inbox.router, prefix="/photos")
    with TestClient(app) as client:
        yield client, service, item


def test_archive_list_update_confirm_conflict_and_missing(api):
    client, service, item = api
    listing = client.get("/photos").json()
    assert listing["total"] == 1 and listing["items"][0]["status"] == "queued"
    assert client.get("/photos/no-such-photo").status_code == 404
    assert client.get("/photos?subject=bogus").status_code == 422
    asyncio.run(service.process_pending())
    detail = client.get(f"/photos/{item['id']}").json()
    assert detail["status"] == "ready"
    url = f"/photos/{item['id']}"
    assert client.patch(url, json={"expected_version": 1, "subject": "english"}).status_code == 409
    changed = client.patch(
        url,
        json={
            "expected_version": detail["version"],
            "note": "Forgot the formula",
        },
    )
    assert changed.status_code == 200 and changed.json()["note"] == "Forgot the formula"
    version = changed.json()["version"]
    assert client.post(url + "/confirm", json={"expected_version": version}).status_code == 422
    confirmed = client.post(url + "/confirm", json={"expected_version": version, "confirmed": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert (
        client.post(
            url + "/retry", json={"expected_version": confirmed.json()["version"]}
        ).status_code
        == 409
    )


def test_supplement_requires_failed_photo_and_valid_image(api):
    client, service, item = api
    url = f"/photos/{item['id']}/supplement"
    response = client.post(
        url,
        data={"expected_version": item["version"]},
        files={"file": ("bad.png", b"broken", "image/png")},
    )
    assert response.status_code == 409
    with service.store.connect() as conn:
        conn.execute("UPDATE photo_inbox_items SET status='failed' WHERE id=?", (item["id"],))
    response = client.post(
        url,
        data={"expected_version": item["version"]},
        files={"file": ("bad.png", b"broken", "image/png")},
    )
    assert response.status_code == 422
    assert service.detail(item["id"])["assets"] == [item["asset"]]


def test_remove_and_restore_are_versioned_and_reversible(api):
    client, _, item = api
    url = f"/photos/{item['id']}"
    removed = client.post(url + "/remove", json={"expected_version": item["version"]})
    assert removed.status_code == 200 and removed.json()["archived_at"] is not None
    assert client.get("/photos").json()["total"] == 0
    assert client.get("/photos?status=removed").json()["total"] == 1
    assert (
        client.post(url + "/restore", json={"expected_version": item["version"]}).status_code == 409
    )
    restored = client.post(url + "/restore", json={"expected_version": removed.json()["version"]})
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    assert client.get("/photos").json()["total"] == 1
