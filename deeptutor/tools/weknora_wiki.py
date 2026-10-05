"""Agent-native navigation over a connected WeKnora Wiki knowledge base.

The generic ``rag`` interface remains the one-shot compatibility surface.  The
two tools here mirror WeKnora's stronger native workflow: search returns small
navigation records, then read hydrates only the pages the model selected.
"""

from __future__ import annotations

from html import escape
from typing import Any

from deeptutor.core.tool_protocol import BaseTool, ToolDefinition, ToolParameter, ToolResult
from deeptutor.knowledge.kb_types import WEKNORA_KB_TYPE
from deeptutor.services.rag.pipelines.weknora.pipeline import (
    DEFAULT_WIKI_TOP_K,
    MAX_WIKI_READ_PAGES,
    MAX_WIKI_TOP_K,
    WeKnoraPipeline,
)
from deeptutor.services.rag.provider_binding import load_kb_config_entry
from deeptutor.tools.prompting import load_prompt_hints


class _PromptHintsMixin:
    def get_prompt_hints(self, language: str = "en"):
        return load_prompt_hints(self.name, language=language)


def _pipeline_for(kb_name: str) -> tuple[WeKnoraPipeline, str]:
    from deeptutor.multi_user.knowledge_access import resolve_for_rag

    resource = resolve_for_rag(kb_name)
    if resource is None:
        raise ValueError(f"Knowledge base '{kb_name}' is not accessible.")
    entry = load_kb_config_entry(resource.base_dir, resource.name)
    if entry.get("type") != WEKNORA_KB_TYPE:
        raise ValueError(f"Knowledge base '{kb_name}' is not connected to WeKnora.")
    return WeKnoraPipeline(str(resource.base_dir)), resource.name


async def search_weknora_wiki(
    *,
    query: str,
    kb_name: str,
    regex: bool | None = None,
    limit: int = DEFAULT_WIKI_TOP_K,
) -> dict[str, Any]:
    pipeline, resolved_name = _pipeline_for(kb_name)
    return await pipeline.search_wiki_pages(
        query,
        resolved_name,
        regex=regex,
        limit=max(1, min(int(limit or DEFAULT_WIKI_TOP_K), MAX_WIKI_TOP_K)),
    )


async def read_weknora_wiki_pages(*, slugs: list[str], kb_name: str) -> dict[str, Any]:
    pipeline, resolved_name = _pipeline_for(kb_name)
    return await pipeline.read_wiki_pages(slugs, resolved_name)


def _render_search_results(result: dict[str, Any]) -> str:
    pages = [page for page in result.get("pages") or [] if isinstance(page, dict)]
    query = escape(str(result.get("query") or ""))
    query_used = escape(str(result.get("query_used") or ""))
    if not pages:
        return f'<search_results count="0" query="{query}" />'
    lines = [f'<search_results count="{len(pages)}" query="{query}" query_used="{query_used}">']
    for page in pages:
        lines.append("<page>")
        for key in ("knowledge_base_id", "slug", "title", "page_type", "summary", "match_snippet"):
            value = page.get(key)
            if value not in (None, "", []):
                lines.append(f"<{key}>{escape(str(value))}</{key}>")
        aliases = page.get("aliases")
        if isinstance(aliases, list) and aliases:
            lines.append(f"<aliases>{escape(', '.join(str(item) for item in aliases))}</aliases>")
        lines.append("</page>")
    lines.append("</search_results>")
    return "\n".join(lines)


class WeKnoraWikiSearchTool(_PromptHintsMixin, BaseTool):
    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="wiki_search",
            description=(
                "Search pages in an attached WeKnora Wiki by title, slug, summary, and content. "
                "The query is a case-insensitive POSIX regular expression: use 'term1|term2' "
                "to match either term, or regex=false for an exact literal phrase. Returns "
                "lightweight page slugs and summaries; use wiki_read_page for full evidence."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description=(
                        "Case-insensitive POSIX expression, preferably an exact title or "
                        "escaped alternatives such as 秋天的怀念|史铁生."
                    ),
                ),
                ToolParameter(
                    name="kb_name",
                    type="string",
                    description="Attached WeKnora knowledge-base name.",
                ),
                ToolParameter(
                    name="regex",
                    type="boolean",
                    description="False forces literal matching; true requires a valid regex.",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description=f"Maximum candidate pages, 1-{MAX_WIKI_TOP_K} (default 5).",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        kb_name = str(kwargs.get("kb_name") or "").strip()
        if not query:
            raise ValueError("wiki_search requires a non-empty query.")
        if not kb_name:
            raise ValueError("wiki_search requires an explicit kb_name.")
        regex = kwargs.get("regex")
        regex = regex if isinstance(regex, bool) else None
        try:
            limit = int(kwargs.get("limit") or DEFAULT_WIKI_TOP_K)
        except (TypeError, ValueError):
            limit = DEFAULT_WIKI_TOP_K
        result = await search_weknora_wiki(
            query=query,
            kb_name=kb_name,
            regex=regex,
            limit=max(1, min(limit, MAX_WIKI_TOP_K)),
        )
        failed = bool(result.get("error_type"))
        content = str(result.get("answer") or "") if failed else _render_search_results(result)
        return ToolResult(content=content, metadata=result, success=not failed)


class WeKnoraWikiReadPageTool(_PromptHintsMixin, BaseTool):
    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="wiki_read_page",
            description=(
                "Read complete WeKnora Wiki pages selected by wiki_search. Returns full Markdown, "
                "source-document references, and Wiki links for focused follow-up navigation."
            ),
            parameters=[
                ToolParameter(
                    name="slugs",
                    type="array",
                    items={"type": "string"},
                    description=f"One to {MAX_WIKI_READ_PAGES} page slugs returned by wiki_search.",
                ),
                ToolParameter(
                    name="kb_name",
                    type="string",
                    description="Attached WeKnora knowledge-base name.",
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_slugs = kwargs.get("slugs")
        slugs = raw_slugs if isinstance(raw_slugs, list) else [raw_slugs]
        slugs = [str(slug or "").strip() for slug in slugs if str(slug or "").strip()]
        kb_name = str(kwargs.get("kb_name") or "").strip()
        if not slugs:
            raise ValueError("wiki_read_page requires at least one slug.")
        if not kb_name:
            raise ValueError("wiki_read_page requires an explicit kb_name.")
        result = await read_weknora_wiki_pages(slugs=slugs, kb_name=kb_name)
        failed = bool(result.get("error_type"))
        sources = [
            {"type": "rag", "kb_name": kb_name, **source}
            for source in (result.get("sources") or [])
            if isinstance(source, dict)
        ]
        return ToolResult(
            content=str(result.get("answer") or result.get("content") or ""),
            sources=sources,
            metadata=result,
            success=not failed,
        )


__all__ = [
    "WeKnoraWikiReadPageTool",
    "WeKnoraWikiSearchTool",
    "read_weknora_wiki_pages",
    "search_weknora_wiki",
]
