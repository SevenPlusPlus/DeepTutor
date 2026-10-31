"""Photo jobs follow the Questions module and background leader lifecycle."""

from types import SimpleNamespace

import pytest

from deeptutor.modules import catalog, services


@pytest.mark.asyncio
async def test_photo_worker_uses_paired_module_callbacks(monkeypatch):
    from deeptutor.services.practice import photo_worker

    events = []
    container = SimpleNamespace(_local_users=lambda: [])

    class Worker:
        def __init__(self, users_provider):
            assert users_provider is container._local_users

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(photo_worker, "PhotoIngestionWorker", Worker)
    monkeypatch.setattr(services, "module_enabled", lambda name: name == "questions")
    starts = services.module_service_callbacks("start", container)
    stops = services.module_service_callbacks("stop", container)
    assert len(starts) == len(stops) == 1
    await starts[0]()
    assert isinstance(container.photo_ingestion_worker, Worker)
    await stops[0]()
    assert events == ["start", "stop"]
    assert container.photo_ingestion_worker is None

    monkeypatch.setattr(services, "module_enabled", lambda name: False)
    assert services.module_service_callbacks("start", container) == []
    assert services.module_service_callbacks("stop", container) == []


def test_photo_router_ships_with_questions():
    assert catalog.module_for_path("deeptutor/api/routers/photo_inbox.py") == "questions"
    assert catalog.module_for_path("deeptutor/agents/question/module_service.py") == "questions"
