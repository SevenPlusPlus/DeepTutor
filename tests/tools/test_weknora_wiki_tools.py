"""WeKnora's native search → read Wiki navigation tools."""

from __future__ import annotations

import pytest

from deeptutor.agents._shared.tool_composition import (
    AUTO_MOUNTED_TOOLS,
    ToolMountFlags,
    compose_enabled_tools,
)
from deeptutor.tools.builtin import BUILTIN_TOOL_NAMES
from deeptutor.tools.weknora_wiki import WeKnoraWikiReadPageTool, WeKnoraWikiSearchTool


class _EmptyRegistry:
    @staticmethod
    def get_enabled(_selected: list[str]) -> list[object]:
        return []


def test_weknora_wiki_tools_are_registered_and_context_gated() -> None:
    assert {"wiki_search", "wiki_read_page"} <= set(BUILTIN_TOOL_NAMES)
    assert {"wiki_search", "wiki_read_page"} <= set(AUTO_MOUNTED_TOOLS)

    absent = compose_enabled_tools(
        registry=_EmptyRegistry(),
        requested_tools=[],
        optional_whitelist=[],
        mount_flags=ToolMountFlags(has_kb=True),
    )
    assert "wiki_search" not in absent
    assert "wiki_read_page" not in absent

    mounted = compose_enabled_tools(
        registry=_EmptyRegistry(),
        requested_tools=[],
        optional_whitelist=[],
        mount_flags=ToolMountFlags(has_kb=True, has_weknora_kb=True),
    )
    assert "wiki_search" in mounted
    assert "wiki_read_page" in mounted


def test_wiki_search_definition_exposes_real_regex_contract() -> None:
    definition = WeKnoraWikiSearchTool().get_definition()
    assert definition.name == "wiki_search"
    assert "POSIX" in definition.description
    assert "|" in definition.description
    assert {parameter.name for parameter in definition.parameters} == {
        "query",
        "kb_name",
        "regex",
        "limit",
    }


@pytest.mark.asyncio
async def test_wiki_search_returns_lightweight_navigation_hits(monkeypatch) -> None:
    async def fake_search(**kwargs):
        assert kwargs == {
            "query": "秋天的怀念|史铁生",
            "kb_name": "mida",
            "regex": True,
            "limit": 5,
        }
        return {
            "query": kwargs["query"],
            "query_used": kwargs["query"],
            "queries_attempted": [kwargs["query"]],
            "pages": [
                {
                    "slug": "秋天的怀念",
                    "title": "《秋天的怀念》",
                    "summary": "史铁生回忆母亲的散文。",
                    "match_snippet": "母亲提出去北海看花。",
                }
            ],
            "provider": "weknora",
        }

    monkeypatch.setattr("deeptutor.tools.weknora_wiki.search_weknora_wiki", fake_search)

    result = await WeKnoraWikiSearchTool().execute(
        query="秋天的怀念|史铁生", kb_name="mida", regex=True, limit=5
    )

    assert result.success is True
    assert "<slug>秋天的怀念</slug>" in result.content
    assert "史铁生回忆母亲" in result.content
    assert result.sources == []


@pytest.mark.asyncio
async def test_wiki_read_page_returns_grounded_sources(monkeypatch) -> None:
    async def fake_read(**kwargs):
        assert kwargs == {"slugs": ["秋天的怀念"], "kb_name": "mida"}
        return {
            "content": "[1] WeKnora Wiki / 《秋天的怀念》\n文章正文",
            "sources": [
                {
                    "id": "page-1",
                    "chunk_id": "wiki:page-1",
                    "slug": "秋天的怀念",
                    "title": "《秋天的怀念》",
                    "content": "文章正文",
                }
            ],
            "provider": "weknora",
        }

    monkeypatch.setattr("deeptutor.tools.weknora_wiki.read_weknora_wiki_pages", fake_read)

    result = await WeKnoraWikiReadPageTool().execute(slugs=["秋天的怀念"], kb_name="mida")

    assert result.success is True
    assert "文章正文" in result.content
    assert result.sources[0]["type"] == "rag"
    assert result.sources[0]["kb_name"] == "mida"
    assert result.sources[0]["slug"] == "秋天的怀念"
