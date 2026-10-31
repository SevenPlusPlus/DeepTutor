"""Question-module lifecycle for durable photo recognition jobs."""

from typing import Any


async def start_photo_ingestion(application_container: Any) -> None:
    """Start polling only while this process owns the background leader lease."""
    from deeptutor.services.practice.photo_worker import PhotoIngestionWorker

    worker = PhotoIngestionWorker(application_container._local_users)
    await worker.start()
    application_container.photo_ingestion_worker = worker


async def stop_photo_ingestion(application_container: Any) -> None:
    """Release the worker before the application container closes."""
    worker = getattr(application_container, "photo_ingestion_worker", None)
    if worker is not None:
        await worker.stop()
        application_container.photo_ingestion_worker = None
