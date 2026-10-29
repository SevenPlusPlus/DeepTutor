from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from deeptutor.multi_user.context import get_current_user
from deeptutor.multi_user.models import CurrentUser
from deeptutor.multi_user.paths import scope_for_user, user_context
from deeptutor.services.practice.photo_worker import PhotoIngestionWorker
from deeptutor.services.workspace import get_content_workspace_service
from deeptutor.services.workspace.context import current_workspace_id


@pytest.fixture
def accounts(tmp_path, monkeypatch):
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
    return [
        CurrentUser(id=name, username=name, role="user", scope=scope_for_user(name, is_admin=False))
        for name in ("photo-a", "photo-b")
    ]


@pytest.mark.asyncio
async def test_worker_restores_accounts_and_skips_archived_scopes(accounts, tmp_path):
    active = {}
    for user in accounts:
        with user_context(user):
            service = get_content_workspace_service()
            active[user.id] = service.create_workspace("Active")["workspace_id"]
            archived = service.create_workspace("Archived")["workspace_id"]
            service.update_workspace(archived, archived=True)

    seen = []

    def factory():
        owner, workspace = get_current_user().id, current_workspace_id()

        async def process_pending(*, limit):
            seen.append((get_current_user().id, current_workspace_id(), limit))
            if owner == accounts[0].id and not workspace:
                raise ValueError("one failed job")

        return SimpleNamespace(
            db_path=tmp_path / owner / (workspace or "default") / "sessions.db",
            process_pending=process_pending,
        )

    worker = PhotoIngestionWorker(lambda: accounts, service_factory=factory)
    before = get_current_user().id, current_workspace_id()
    await worker.process_once()
    assert seen == [
        (accounts[0].id, "", 1),
        (accounts[0].id, active[accounts[0].id], 1),
        (accounts[1].id, "", 1),
        (accounts[1].id, active[accounts[1].id], 1),
    ]
    assert (get_current_user().id, current_workspace_id()) == before


@pytest.mark.asyncio
async def test_worker_start_is_idempotent_and_stop_cancels_inflight_job(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    worker = PhotoIngestionWorker(lambda: [])

    async def process_once():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker, "process_once", process_once)
    await worker.start()
    first = worker._task
    await worker.start()
    assert worker._task is first
    await asyncio.wait_for(entered.wait(), timeout=1)
    await worker.stop()
    assert cancelled.is_set()
    assert worker._task is None
    await worker.stop()
