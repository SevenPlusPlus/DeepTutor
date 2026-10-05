"""Retrieval-only RAG pipeline backed by an external WeKnora knowledge base."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from deeptutor.runtime.home import get_runtime_data_root
from deeptutor.services.rag.provider_binding import load_kb_config_entry

from .config import (
    WeKnoraNotConfiguredError,
    capabilities_from_knowledge_base,
    config_from_entry,
)

logger = logging.getLogger(__name__)

PROVIDER = "weknora"
DEFAULT_KB_BASE_DIR = str(get_runtime_data_root() / "knowledge_bases")
DEFAULT_WIKI_TOP_K = 5
MAX_WIKI_TOP_K = 20
MAX_WIKI_PAGE_CHARS = 24_000
MAX_WIKI_CONTEXT_CHARS = 72_000


class WeKnoraPipeline:
    def __init__(self, kb_base_dir: Optional[str] = None, *, client_factory=None, **_: Any) -> None:
        self.kb_base_dir = kb_base_dir or DEFAULT_KB_BASE_DIR
        self._client_factory = client_factory

    def _client(self, config):
        if self._client_factory is not None:
            return self._client_factory(config)
        from .client import WeKnoraClient

        return WeKnoraClient(config)

    async def search(self, query: str, kb_name: str, **kwargs) -> Dict[str, Any]:
        try:
            entry = load_kb_config_entry(self.kb_base_dir, kb_name)
            config = config_from_entry(entry)
        except WeKnoraNotConfiguredError as exc:
            return self._error_result(query, exc, error_type="not_configured")

        client = self._client(config)
        try:
            capabilities = await self._capabilities(client, config.capabilities)
        except Exception as exc:
            logger.warning("Could not refresh WeKnora capabilities for '%s': %s", kb_name, exc)
            capabilities = config.capabilities or {}

        # A pre-capabilities WeKnora server (or a pointer saved by an older
        # DeepTutor) keeps the historical document-search behaviour.  Once the
        # server says Wiki is enabled, however, Wiki pages are a first-class
        # retrieval surface even when vector/keyword RAG is disabled.
        wiki_enabled = bool(capabilities.get("wiki"))
        rag_enabled = (
            bool(capabilities.get("vector") or capabilities.get("keyword"))
            if capabilities
            else True
        )

        try:
            result = await self._retrieve(
                client,
                query,
                wiki_enabled=wiki_enabled,
                rag_enabled=rag_enabled,
                top_k=self._top_k(kwargs),
            )
        except Exception as exc:
            logger.error("WeKnora search failed for '%s': %s", kb_name, exc)
            return self._error_result(query, exc, error_type="retrieval_error")

        return {
            "query": query,
            "answer": result["content"],
            "content": result["content"],
            "sources": result["sources"],
            "provider": PROVIDER,
            "weknora_capabilities": capabilities,
            **({"warnings": result["warnings"]} if result["warnings"] else {}),
        }

    @staticmethod
    async def _capabilities(client, saved: dict[str, bool] | None) -> dict[str, bool]:
        current = capabilities_from_knowledge_base(await client.get_knowledge_base())
        # Old WeKnora versions did not advertise feature flags.  Keep the
        # connection-time snapshot in that case; current servers remain live
        # with configuration changes made after the KB was connected.
        return current or saved or {}

    @staticmethod
    def _top_k(kwargs: dict[str, Any]) -> int:
        try:
            requested = int(kwargs.get("top_k") or DEFAULT_WIKI_TOP_K)
        except (TypeError, ValueError):
            return DEFAULT_WIKI_TOP_K
        return max(1, min(requested, MAX_WIKI_TOP_K))

    async def _retrieve(
        self,
        client,
        query: str,
        *,
        wiki_enabled: bool,
        rag_enabled: bool,
        top_k: int,
    ) -> dict[str, Any]:
        jobs: list[tuple[str, Any]] = []
        if wiki_enabled:
            jobs.append(("wiki", self._wiki_sources(client, query, top_k=top_k)))
        if rag_enabled:
            jobs.append(("rag", client.search(query)))
        if not jobs:
            raise RuntimeError(
                "This WeKnora knowledge base has neither Wiki nor RAG retrieval enabled."
            )

        outcomes = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        sources: list[dict[str, Any]] = []
        warnings: list[str] = []
        for (kind, _), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                warnings.append(f"{kind}: {outcome}")
                continue
            if kind == "wiki":
                sources.extend(outcome)
            else:
                sources.extend(self._document_sources(outcome))

        if not sources and warnings:
            raise RuntimeError("; ".join(warnings))
        content = (
            self._render_context(sources)
            if wiki_enabled
            else "\n\n---\n\n".join(str(source.get("content") or "") for source in sources)
        )
        return {"content": content, "sources": sources, "warnings": warnings}

    async def _wiki_sources(self, client, query: str, *, top_k: int) -> list[dict[str, Any]]:
        hits = await client.search_wiki(query, limit=top_k)
        pages = await asyncio.gather(
            *(client.get_wiki_page(str(hit.get("slug") or "")) for hit in hits),
            return_exceptions=True,
        )
        sources: list[dict[str, Any]] = []
        for hit, page in zip(hits, pages):
            hydrated = page if isinstance(page, dict) else {}
            content = str(
                hydrated.get("content")
                or hit.get("match_snippet")
                or hit.get("summary")
                or ""
            ).strip()
            if not content:
                continue
            content = content[:MAX_WIKI_PAGE_CHARS]
            slug = str(hydrated.get("slug") or hit.get("slug") or "").strip()
            title = str(hydrated.get("title") or hit.get("title") or slug).strip()
            page_id = str(hydrated.get("id") or hit.get("id") or slug).strip()
            sources.append(
                {
                    "id": page_id,
                    "chunk_id": f"wiki:{page_id}",
                    "title": title,
                    "content": content,
                    "source": f"WeKnora Wiki / {title}",
                    "knowledge_base_id": str(
                        hydrated.get("knowledge_base_id")
                        or hit.get("knowledge_base_id")
                        or ""
                    ),
                    "slug": slug,
                    "page_type": str(
                        hydrated.get("page_type") or hit.get("page_type") or ""
                    ),
                    "summary": str(hydrated.get("summary") or hit.get("summary") or ""),
                    "aliases": hydrated.get("aliases") or hit.get("aliases") or [],
                    "source_refs": hydrated.get("source_refs") or [],
                    "chunk_refs": hydrated.get("chunk_refs") or [],
                    "weknora_source_type": "wiki_page",
                }
            )
        return sources

    @staticmethod
    def _document_sources(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for chunk in chunks:
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            source = dict(chunk)
            title = str(
                source.get("knowledge_title")
                or source.get("knowledge_filename")
                or source.get("title")
                or "WeKnora document"
            )
            source.setdefault("title", title)
            source.setdefault("source", f"WeKnora / {title}")
            source.setdefault("chunk_id", str(source.get("id") or ""))
            source["weknora_source_type"] = "document_chunk"
            sources.append(source)
        return sources

    @staticmethod
    def _render_context(sources: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        remaining = MAX_WIKI_CONTEXT_CHARS
        for index, source in enumerate(sources, start=1):
            body = str(source.get("content") or "").strip()
            if not body or remaining <= 0:
                break
            heading = str(source.get("source") or source.get("title") or f"Source {index}")
            block = f"[{index}] {heading}\n{body}"
            if len(block) > remaining:
                block = block[:remaining]
            parts.append(block)
            remaining -= len(block)
        return "\n\n---\n\n".join(parts)

    def _error_result(self, query: str, exc: Exception, *, error_type: str) -> Dict[str, Any]:
        return {
            "query": query,
            "answer": str(exc),
            "content": "",
            "sources": [],
            "provider": PROVIDER,
            "error_type": error_type,
        }

    async def initialize(self, kb_name: str, file_paths: List[str], **kwargs) -> bool:
        raise RuntimeError(
            "WeKnora knowledge bases are managed in WeKnora; DeepTutor does not "
            "upload or index their documents."
        )

    async def add_documents(self, kb_name: str, file_paths: List[str], **kwargs) -> bool:
        return await self.initialize(kb_name, file_paths, **kwargs)

    async def delete(self, kb_name: str, **kwargs) -> bool:
        return True


__all__ = ["WeKnoraPipeline", "PROVIDER"]
