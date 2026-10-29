"""Leader-owned polling of durable photo jobs in their original content scopes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


class PhotoIngestionWorker:
    """Process one photo per workspace each cycle so continuous uploads can queue.

    The database owns job leases. This task only supplies lifecycle and restores
    the authenticated account/workspace context before resolving a service.
    """

    def __init__(
        self,
        users_provider: Callable[[], Iterable[Any]],
        *,
        interval_seconds: float = 2.0,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.users_provider = users_provider
        self.interval_seconds = max(0.1, interval_seconds)
        self.service_factory = service_factory
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="photo-ingestion-worker")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.process_once()
            except Exception:
                logger.exception("Photo ingestion worker cycle failed")
            await asyncio.sleep(self.interval_seconds)

    async def process_once(self) -> None:
        from deeptutor.multi_user.paths import user_context
        from deeptutor.services.workspace import get_content_workspace_service
        from deeptutor.services.workspace.activity import data_activity
        from deeptutor.services.workspace.context import workspace_context

        if self.service_factory is None:
            from deeptutor.services.practice.photo_ingestion import get_photo_ingestion_service

            service_factory = get_photo_ingestion_service
        else:
            service_factory = self.service_factory

        seen: set[str] = set()
        for user in self.users_provider():
            try:
                with user_context(user), data_activity():
                    catalog = get_content_workspace_service().list_workspaces()
                    workspace_ids = [""] + [
                        row["workspace_id"]
                        for row in catalog
                        if row["kind"] == "workspace"
                        and not row.get("archived")
                        and row.get("status") == "ready"
                    ]
                    for workspace_id in dict.fromkeys(workspace_ids):
                        try:
                            with workspace_context(workspace_id):
                                service = service_factory()
                                key = str(service.db_path.resolve())
                                if key in seen:
                                    continue
                                seen.add(key)
                                await service.process_pending(limit=1)
                        except Exception:
                            # An offline workspace or a failed job must not stop
                            # queued photos in another user's workspace.
                            logger.exception("Photo ingestion workspace cycle failed")
            except Exception:
                logger.exception("Photo ingestion account cycle failed")
