"""Account photo navigation must find photos saved to a named learning space."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from deeptutor.api.routers import photo_inbox
from deeptutor.multi_user.models import CurrentUser
from deeptutor.multi_user.paths import scope_for_user, user_context
from deeptutor.services.practice.photo_ingestion import PhotoIngestionService
from deeptutor.services.session import get_sqlite_session_store
from deeptutor.services.workspace import WorkspaceError, get_content_workspace_service
from deeptutor.services.workspace.context import current_workspace_id, workspace_context


class Extractor:
    async def extract(self, request):
        return [{"question": "Find x", "subject": "math", "answer_origin": "missing"}]


@pytest.fixture
def api(tmp_path, monkeypatch):
    from deeptutor.multi_user import identity, paths

    root = tmp_path / "data"
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paths, "ADMIN_WORKSPACE_ROOT", root)
    monkeypatch.setattr(paths, "USERS_ROOT", root / "users")
    monkeypatch.setattr(paths, "SYSTEM_ROOT", root / "system")
    monkeypatch.setattr(paths, "_path_services", {})
    monkeypatch.setattr(identity, "AUTH_DIR", root / "system" / "auth")
    monkeypatch.setattr(identity, "USERS_FILE", root / "system" / "auth" / "users.json")
    monkeypatch.delenv("DEEPTUTOR_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("DEEPTUTOR_WORKSPACE_ALLOWED_ROOTS", raising=False)
    users = {
        name: CurrentUser(
            id=name, username=name, role="user", scope=scope_for_user(name, is_admin=False)
        )
        for name in ("photo-alice", "photo-bob")
    }
    spaces = {}
    for name, user in users.items():
        with user_context(user):
            spaces[name] = get_content_workspace_service().create_workspace(f"{name} learning")

    def service():
        return PhotoIngestionService(
            get_sqlite_session_store().db_path,
            Extractor(),
            workspace_key=current_workspace_id() or "default",
        )

    def seed(owner, workspace_id, number, received_at, *, ready=False, local_id=None):
        with user_context(users[owner]), workspace_context(workspace_id):
            store = service()
            image = io.BytesIO()
            Image.new("RGB", (8, 8), (number % 256, number // 256, 0)).save(image, format="PNG")
            item = asyncio.run(
                store.ingest(
                    data=image.getvalue(),
                    filename=f"photo-{number}.png",
                    mime_type="image/png",
                    source={"event_key": f"wechat-{number}", "channel": "weixin"},
                    received_at=received_at,
                )
            )
            if ready:
                asyncio.run(store.process_pending())
            if local_id is not None:
                with store.store.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("PRAGMA defer_foreign_keys=ON")
                    conn.execute(
                        "UPDATE photo_inbox_items SET id=? WHERE id=?", (local_id, item["id"])
                    )
                    conn.execute(
                        "UPDATE photo_inbox_receipts SET item_id=? WHERE item_id=?",
                        (local_id, item["id"]),
                    )
                item["id"] = local_id
            return store.detail(item["id"])

    monkeypatch.setattr(photo_inbox, "get_photo_ingestion_service", service)
    app = FastAPI()

    @app.middleware("http")
    async def scope(request, call_next):
        owner = request.headers.get("x-test-owner", "photo-alice")
        try:
            with (
                user_context(users[owner]),
                workspace_context(request.query_params.get("dt_workspace", "")),
            ):
                return await call_next(request)
        except WorkspaceError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})

    app.include_router(photo_inbox.router, prefix="/photos")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, users=users, spaces=spaces, seed=seed)


def test_account_overview_finds_saved_named_workspace_photo_but_mutations_stay_scoped(api):
    space = api.spaces["photo-alice"]
    photo = api.seed("photo-alice", space["workspace_id"], 1, 1_700_000_000, ready=True)
    assert api.client.get("/photos").json()["total"] == 0
    account = api.client.get("/photos?all_workspaces=true").json()
    assert account["total"] == 1
    assert account["counts"] == {"photos": 1, "pending": 0, "confirmed": 0}
    assert account["items"][0]["id"] == photo["id"]
    assert account["items"][0]["content_workspace_id"] == space["workspace_id"]
    assert account["items"][0]["content_workspace_name"] == space["display_name"]
    assert account["unavailable_workspaces"] == []
    assert api.client.get(f"/photos/{photo['id']}").status_code == 404
    payload = {"expected_version": photo["version"], "note": "Reviewed in original space"}
    assert api.client.patch(f"/photos/{photo['id']}", json=payload).status_code == 404
    scoped = f"/photos/{photo['id']}?dt_workspace={space['workspace_id']}"
    assert api.client.get(scoped).status_code == 200
    assert api.client.patch(scoped, json=payload).json()["note"] == payload["note"]
    assert api.client.get("/photos").json()["total"] == 0


def test_account_navigation_preserves_same_local_ids_and_excludes_other_accounts(api):
    space = api.spaces["photo-alice"]["workspace_id"]
    api.seed("photo-alice", "", 1, 100, local_id="shared-photo")
    api.seed("photo-alice", space, 2, 200, local_id="shared-photo")
    api.seed("photo-bob", api.spaces["photo-bob"]["workspace_id"], 3, 300)
    account = api.client.get("/photos?all_workspaces=true").json()
    assert account["total"] == 2
    assert account["counts"] == {"photos": 2, "pending": 2, "confirmed": 0}
    assert [(item["id"], item["content_workspace_id"]) for item in account["items"]] == [
        ("shared-photo", space),
        ("shared-photo", ""),
    ]
    bob = api.client.get(
        "/photos?all_workspaces=true", headers={"x-test-owner": "photo-bob"}
    ).json()
    assert bob["total"] == 1
    assert bob["items"][0]["content_workspace_id"] == api.spaces["photo-bob"]["workspace_id"]
    foreign = api.spaces["photo-bob"]["workspace_id"]
    assert api.client.get(f"/photos?dt_workspace={foreign}").status_code == 404


def test_global_pagination_reads_beyond_each_workspace_page_cap_and_keeps_filters(api):
    space = api.spaces["photo-alice"]["workspace_id"]
    for number in range(1, 106):
        api.seed("photo-alice", space, number, 1000 + number)
    for number in range(106, 109):
        api.seed("photo-alice", "", number, 2000 + number)
    response = api.client.get("/photos?all_workspaces=true&offset=100&limit=8").json()
    assert response["total"] == 108
    assert len(response["items"]) == 8
    assert [item["received_at"] for item in response["items"]] == list(range(1008, 1000, -1))
    filtered = api.client.get(
        "/photos?all_workspaces=true&since=1100&until=2107&status=pending"
    ).json()
    assert filtered["total"] == 7
    assert [item["received_at"] for item in filtered["items"]] == [
        2106,
        1105,
        1104,
        1103,
        1102,
        1101,
        1100,
    ]
    assert filtered["counts"]["photos"] == 108


def test_unavailable_workspace_does_not_hide_another_saved_photo(api, tmp_path):
    api.seed("photo-alice", "", 1, 100)
    offline = api.spaces["photo-alice"]
    api.seed("photo-alice", offline["workspace_id"], 2, 200)
    Path(offline["path"]).rename(tmp_path / "disconnected-learning-folder")
    result = api.client.get("/photos?all_workspaces=true").json()
    assert result["total"] == 1
    assert result["items"][0]["content_workspace_id"] == ""
    assert result["unavailable_workspaces"] == [offline["workspace_id"]]


def test_invalid_filters_are_rejected_instead_of_becoming_empty_success(api):
    assert api.client.get("/photos?all_workspaces=true&subject=not-a-subject").status_code == 422
    assert api.client.get("/photos?all_workspaces=true&status=not-a-status").status_code == 422
    assert api.client.get("/photos?all_workspaces=true&since=nan").status_code == 422
