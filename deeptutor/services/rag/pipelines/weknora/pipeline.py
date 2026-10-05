"""Retrieval-only RAG pipeline backed by an external WeKnora knowledge base."""

from __future__ import annotations

import asyncio
import logging
import re
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
MAX_WIKI_READ_PAGES = 5

_SOURCE_INSTRUCTION_RE = re.compile(
    r"^(?:请)?(?:仅)?(?:根据|基于).{0,48}?(?:知识库|资料|文档)(?:回答|说明)?[：:]\s*"
)
_TITLE_QUESTION_RE = re.compile(
    r"(?P<title>[^？?！!，,。；;：:\n]{2,64}?)(?:主要)?(?:讲了什么|讲述了什么|"
    r"内容(?:是|概括|概要)|概括|概述|简介|是什么|有哪些|怎么样|如何|为什么)"
)
_QUOTED_TITLE_RE = re.compile(r"《([^》]{1,80})》|[\"“]([^\"”]{1,80})[\"”]")
_QUERY_SPLIT_RE = re.compile(r"[\s,，、;；:：?？!！。]+")
_GENERIC_QUERY_TERMS = frozenset(
    {
        "请",
        "回答",
        "知识库",
        "主要讲了什么",
        "讲了什么",
        "内容",
        "内容概括",
        "内容概要",
        "概括",
        "概述",
        "简介",
        "课文",
        "散文",
    }
)


def _wiki_query_candidates(query: str, *, regex: bool | None) -> list[str]:
    """Build ordered v0.8-compatible Wiki queries.

    WeKnora's native Agent tells the model that Wiki queries are POSIX regular
    expressions.  DeepTutor's generic ``rag`` interface historically promised
    natural language instead, so the adapter also supplies a deterministic
    safety net: exact quoted/title subjects first, then an OR expression for
    explicit keyword lists.  The fallback runs only after the original query
    misses and never changes an explicitly literal/regex request.
    """

    raw = str(query or "").strip()
    if not raw:
        return []
    if regex is False:
        return [re.escape(raw)]
    if regex is True:
        try:
            re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid Wiki regular expression {raw!r}: {exc}") from exc
        return [raw]

    candidates = [raw]
    tail = _SOURCE_INSTRUCTION_RE.sub("", raw).strip()

    for match in _QUOTED_TITLE_RE.finditer(tail):
        _append_query_candidate(candidates, match.group(1) or match.group(2) or "")

    title_match = _TITLE_QUESTION_RE.search(tail)
    if title_match is not None:
        title = title_match.group("title").strip().strip("《》\"“”'‘’ ")
        _append_query_candidate(candidates, title)

    terms: list[str] = []
    for part in _QUERY_SPLIT_RE.split(tail):
        term = part.strip().strip("《》\"“”'‘’()（）[]【】")
        nested = _TITLE_QUESTION_RE.search(term)
        if nested is not None:
            term = nested.group("title").strip().strip("《》\"“”'‘’ ")
        if len(term) < 2 or term in _GENERIC_QUERY_TERMS or term in terms:
            continue
        terms.append(term)

    if len(terms) > 1:
        _append_query_candidate(candidates, "|".join(re.escape(term) for term in terms[:6]))
    for term in terms[:6]:
        _append_query_candidate(candidates, re.escape(term))
    return candidates


def _append_query_candidate(candidates: list[str], candidate: str) -> None:
    normalized = str(candidate or "").strip()
    if len(normalized) >= 2 and normalized not in candidates:
        candidates.append(normalized)


class WeKnoraPipeline:
    def __init__(self, kb_base_dir: Optional[str] = None, *, client_factory=None, **_: Any) -> None:
        self.kb_base_dir = kb_base_dir or DEFAULT_KB_BASE_DIR
        self._client_factory = client_factory

    def _client(self, config):
        if self._client_factory is not None:
            return self._client_factory(config)
        from .client import WeKnoraClient

        return WeKnoraClient(config)

    async def _open(self, kb_name: str):
        entry = load_kb_config_entry(self.kb_base_dir, kb_name)
        config = config_from_entry(entry)
        client = self._client(config)
        try:
            capabilities = await self._capabilities(client, config.capabilities)
        except Exception as exc:
            logger.warning("Could not refresh WeKnora capabilities for '%s': %s", kb_name, exc)
            capabilities = config.capabilities or {}
        return client, capabilities

    async def search(self, query: str, kb_name: str, **kwargs) -> Dict[str, Any]:
        try:
            client, capabilities = await self._open(kb_name)
        except WeKnoraNotConfiguredError as exc:
            return self._error_result(query, exc, error_type="not_configured")

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
            **({"weknora_wiki_queries": result["wiki_queries"]} if result["wiki_queries"] else {}),
            **({"warnings": result["warnings"]} if result["warnings"] else {}),
        }

    async def search_wiki_pages(
        self,
        query: str,
        kb_name: str,
        *,
        limit: int = DEFAULT_WIKI_TOP_K,
        regex: bool | None = None,
    ) -> dict[str, Any]:
        """Locate Wiki pages without hydrating their full bodies.

        This is the search half of WeKnora's native agent workflow.  It owns
        the provider's POSIX-regex semantics and deterministic natural-language
        fallback so callers only need to choose a query and a knowledge base.
        """

        query = str(query or "").strip()
        if not query:
            raise ValueError("WeKnora Wiki search requires a non-empty query.")
        try:
            client, capabilities = await self._open(kb_name)
            self._require_wiki(capabilities)
            pages, query_used, attempted = await self._search_wiki_hits(
                client,
                query,
                limit=max(1, min(int(limit or DEFAULT_WIKI_TOP_K), MAX_WIKI_TOP_K)),
                regex=regex,
            )
        except WeKnoraNotConfiguredError as exc:
            return self._error_result(query, exc, error_type="not_configured")
        except Exception as exc:
            logger.error("WeKnora Wiki search failed for '%s': %s", kb_name, exc)
            return self._error_result(query, exc, error_type="retrieval_error")
        return {
            "query": query,
            "query_used": query_used,
            "queries_attempted": attempted,
            "pages": [self._wiki_hit(page) for page in pages],
            "provider": PROVIDER,
            "weknora_capabilities": capabilities,
        }

    async def read_wiki_pages(self, slugs: list[str], kb_name: str) -> dict[str, Any]:
        """Read a bounded set of Wiki pages selected by ``search_wiki_pages``."""

        normalized = list(
            dict.fromkeys(str(slug or "").strip().strip("/") for slug in slugs if str(slug).strip())
        )[:MAX_WIKI_READ_PAGES]
        if not normalized:
            raise ValueError("WeKnora Wiki page read requires at least one slug.")
        try:
            client, capabilities = await self._open(kb_name)
            self._require_wiki(capabilities)
            pages = await asyncio.gather(
                *(client.get_wiki_page(slug) for slug in normalized),
                return_exceptions=True,
            )
        except WeKnoraNotConfiguredError as exc:
            return self._error_result("", exc, error_type="not_configured")
        except Exception as exc:
            logger.error("WeKnora Wiki page read failed for '%s': %s", kb_name, exc)
            return self._error_result("", exc, error_type="retrieval_error")

        sources: list[dict[str, Any]] = []
        warnings: list[str] = []
        for slug, page in zip(normalized, pages):
            if isinstance(page, BaseException):
                warnings.append(f"{slug}: {page}")
                continue
            source = self._wiki_source({"slug": slug}, page)
            if source is not None:
                sources.append(source)
        if not sources and warnings:
            return self._error_result(
                "", RuntimeError("; ".join(warnings)), error_type="retrieval_error"
            )
        content = self._render_context(sources)
        return {
            "query": "",
            "answer": content,
            "content": content,
            "sources": sources,
            "provider": PROVIDER,
            "weknora_capabilities": capabilities,
            **({"warnings": warnings} if warnings else {}),
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
        wiki_queries: list[str] = []
        for (kind, _), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                warnings.append(f"{kind}: {outcome}")
                continue
            if kind == "wiki":
                sources.extend(outcome["sources"])
                wiki_queries.extend(outcome["queries_attempted"])
            else:
                sources.extend(self._document_sources(outcome))

        if not sources and warnings:
            raise RuntimeError("; ".join(warnings))
        content = (
            self._render_context(sources)
            if wiki_enabled
            else "\n\n---\n\n".join(str(source.get("content") or "") for source in sources)
        )
        return {
            "content": content,
            "sources": sources,
            "warnings": warnings,
            "wiki_queries": wiki_queries,
        }

    async def _wiki_sources(self, client, query: str, *, top_k: int) -> dict[str, Any]:
        hits, _query_used, attempted = await self._search_wiki_hits(
            client, query, limit=top_k, regex=None
        )
        pages = await asyncio.gather(
            *(client.get_wiki_page(str(hit.get("slug") or "")) for hit in hits),
            return_exceptions=True,
        )
        sources: list[dict[str, Any]] = []
        for hit, page in zip(hits, pages):
            hydrated = page if isinstance(page, dict) else {}
            source = self._wiki_source(hit, hydrated)
            if source is not None:
                sources.append(source)
        return {"sources": sources, "queries_attempted": attempted}

    @staticmethod
    def _require_wiki(capabilities: dict[str, bool]) -> None:
        if capabilities and not capabilities.get("wiki"):
            raise RuntimeError("This WeKnora knowledge base does not have Wiki enabled.")

    @staticmethod
    async def _search_wiki_hits(
        client,
        query: str,
        *,
        limit: int,
        regex: bool | None,
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        attempted: list[str] = []
        for candidate in _wiki_query_candidates(query, regex=regex):
            attempted.append(candidate)
            hits = await client.search_wiki(candidate, limit=limit)
            if hits:
                return hits, candidate, attempted
        return [], attempted[-1] if attempted else query, attempted

    @staticmethod
    def _wiki_hit(page: dict[str, Any]) -> dict[str, Any]:
        return {
            key: page[key]
            for key in (
                "id",
                "knowledge_base_id",
                "slug",
                "title",
                "page_type",
                "summary",
                "aliases",
                "match_snippet",
            )
            if page.get(key) not in (None, "", [])
        }

    @staticmethod
    def _wiki_source(hit: dict[str, Any], hydrated: dict[str, Any]) -> dict[str, Any] | None:
        content = str(
            hydrated.get("content") or hit.get("match_snippet") or hit.get("summary") or ""
        ).strip()
        if not content:
            return None
        content = content[:MAX_WIKI_PAGE_CHARS]
        slug = str(hydrated.get("slug") or hit.get("slug") or "").strip()
        title = str(hydrated.get("title") or hit.get("title") or slug).strip()
        page_id = str(hydrated.get("id") or hit.get("id") or slug).strip()
        source = {
            "id": page_id,
            "chunk_id": f"wiki:{page_id}",
            "title": title,
            "content": content,
            "source": f"WeKnora Wiki / {title}",
            "knowledge_base_id": str(
                hydrated.get("knowledge_base_id") or hit.get("knowledge_base_id") or ""
            ),
            "slug": slug,
            "page_type": str(hydrated.get("page_type") or hit.get("page_type") or ""),
            "summary": str(hydrated.get("summary") or hit.get("summary") or ""),
            "aliases": hydrated.get("aliases") or hit.get("aliases") or [],
            "source_refs": hydrated.get("source_refs") or hit.get("source_refs") or [],
            "chunk_refs": hydrated.get("chunk_refs") or hit.get("chunk_refs") or [],
            "weknora_source_type": "wiki_page",
        }
        for key in ("out_links", "in_links"):
            value = hydrated.get(key) or hit.get(key)
            if value:
                source[key] = value
        return source

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
