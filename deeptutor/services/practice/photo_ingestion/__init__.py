"""Durable photo collection, independent of temporary recognition previews."""

from .service import SUBJECTS, PhotoIngestionService, get_photo_ingestion_service

__all__ = ["PhotoIngestionService", "SUBJECTS", "get_photo_ingestion_service"]
