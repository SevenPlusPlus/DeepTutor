"""Tests for the retrieval-only Tencent WeKnora pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from deeptutor.knowledge.kb_types import CONNECTED_KB_TYPES, WEKNORA_KB_TYPE
from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.services.rag.factory import get_pipeline, list_pipelines, normalize_provider_name
from deeptutor.services.rag.pipelines.weknora.client import (
    MAX_RESPONSE_BYTES,
    WeKnoraAPIError,
    WeKnoraClient,
)
from deeptutor.services.rag.pipelines.weknora.config import (
    capabilities_from_knowledge_base,
    config_from_entry,
)
from deeptutor.services.rag.pipelines.weknora.pipeline import WeKnoraPipeline
from deeptutor.services.rag.pipelines.weknora.probe import probe_weknora


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret"
        if request.url.path == "/api/v1/knowledge-bases":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"id": "kb-1", "name": "Research"},
                        {"id": "kb-2", "name": "Operations"},
                    ],
                },
            )
        if request.url.path == "/api/v1/knowledge-search":
            body = json.loads(request.content)
            assert body == {"query": "what is AI?", "knowledge_base_id": "kb-1"}
            assert request.url.params["resource_urls"] == "handle"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "chunk-1",
                            "content": "Context one",
                            "knowledge_id": "knowledge-1",
                            "knowledge_title": "Guide",
                            "knowledge_filename": "guide.pdf",
                            "score": 0.9,
                        },
                        {"id": "chunk-2", "content": "Context two"},
                    ],
                },
            )
        return httpx.Response(404, json={"success": False})

    return httpx.MockTransport(handler)


def _config() -> object:
    return config_from_entry(
        {
            "server_url": "http://localhost:8080/",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        }
    )


def _wiki_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret"
        if request.url.path == "/api/v1/wiki-search":
            assert json.loads(request.content) == {
                "query": "what is AI?",
                "knowledge_base_id": "kb-1",
                "limit": 5,
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "page-1",
                            "knowledge_base_id": "kb-1",
                            "slug": "concept/artificial-intelligence",
                            "title": "Artificial intelligence",
                            "page_type": "concept",
                            "summary": "A field of computer science.",
                            "match_snippet": "AI builds intelligent systems.",
                        }
                    ],
                },
            )
        if request.url.path == (
            "/api/v1/knowledgebase/kb-1/wiki/pages/concept/artificial-intelligence"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "page-1",
                    "knowledge_base_id": "kb-1",
                    "slug": "concept/artificial-intelligence",
                    "title": "Artificial intelligence",
                    "page_type": "concept",
                    "content": "# Artificial intelligence\n\nAI builds intelligent systems.",
                    "summary": "A field of computer science.",
                    "source_refs": ["doc-1|AI Guide"],
                    "chunk_refs": ["chunk-1"],
                },
            )
        return httpx.Response(404, json={"success": False})

    return httpx.MockTransport(handler)


def test_config_requires_complete_binding() -> None:
    config = _config()
    assert config.base_url == "http://localhost:8080"
    assert config.knowledge_base_id == "kb-1"

    with pytest.raises(Exception, match="knowledge base ID"):
        config_from_entry({"server_url": "http://x", "api_key": "secret"})
    with pytest.raises(Exception, match="API key"):
        config_from_entry({"server_url": "http://x", "knowledge_base_id": "kb-1"})
    with pytest.raises(Exception, match="server URL"):
        config_from_entry(
            {
                "server_url": "ftp://example.com",
                "api_key": "secret",
                "knowledge_base_id": "kb-1",
            }
        )
    with pytest.raises(Exception, match="server URL"):
        config_from_entry(
            {
                "server_url": "http://user:password@example.com",
                "api_key": "secret",
                "knowledge_base_id": "kb-1",
            }
        )


def test_capabilities_detect_wiki_only_knowledge_base() -> None:
    assert capabilities_from_knowledge_base(
        {
            "type": "document",
            "indexing_strategy": {
                "vector_enabled": False,
                "keyword_enabled": False,
                "wiki_enabled": True,
                "graph_enabled": False,
            },
        }
    ) == {
        "vector": False,
        "keyword": False,
        "wiki": True,
        "graph": False,
        "faq": False,
    }


def test_capabilities_preserve_legacy_document_search_without_feature_flags() -> None:
    assert capabilities_from_knowledge_base({"type": "document"}) == {}


def test_client_search_uses_official_endpoint() -> None:
    result = asyncio.run(WeKnoraClient(_config(), transport=_transport()).search("what is AI?"))
    assert [item["id"] for item in result] == ["chunk-1", "chunk-2"]


def test_client_searches_and_reads_native_wiki_pages() -> None:
    client = WeKnoraClient(_config(), transport=_wiki_transport())
    hits = asyncio.run(client.search_wiki("what is AI?"))
    assert hits[0]["slug"] == "concept/artificial-intelligence"
    page = asyncio.run(client.get_wiki_page(hits[0]["slug"]))
    assert page["content"].startswith("# Artificial intelligence")


def test_client_falls_back_to_v082_single_kb_wiki_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret"
        if request.url.path == "/api/v1/wiki-search":
            return httpx.Response(404, text="404 page not found")
        if request.url.path == "/api/v1/knowledgebase/kb-1/wiki/search":
            assert request.url.params["q"] == "what is AI?"
            assert request.url.params["limit"] == "5"
            return httpx.Response(
                200,
                json={
                    "pages": [
                        {
                            "id": "page-1",
                            "knowledge_base_id": "kb-1",
                            "slug": "concept/artificial-intelligence",
                            "title": "Artificial intelligence",
                            "content": "# Artificial intelligence",
                        }
                    ]
                },
            )
        return httpx.Response(404, text="unexpected route")

    client = WeKnoraClient(_config(), transport=httpx.MockTransport(handler))
    hits = asyncio.run(client.search_wiki("what is AI?"))
    assert hits[0]["slug"] == "concept/artificial-intelligence"


def test_client_bounds_external_response_bodies() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-length": str(MAX_RESPONSE_BYTES + 1)},
            content=b"{}",
        )
    )
    with pytest.raises(WeKnoraAPIError, match="4 MiB"):
        asyncio.run(WeKnoraClient(_config(), transport=transport).list_knowledge_bases())


def test_client_revalidates_saved_targets_before_retrieval() -> None:
    config = config_from_entry(
        {
            "server_url": "http://169.254.169.254",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        }
    )
    transport = httpx.MockTransport(
        lambda _request: pytest.fail("unsafe target reached the HTTP transport")
    )
    with pytest.raises(WeKnoraAPIError, match="Unsafe WeKnora server URL"):
        asyncio.run(WeKnoraClient(config, transport=transport).list_knowledge_bases())


def test_probe_validates_visible_knowledge_base() -> None:
    probe = asyncio.run(
        probe_weknora(
            "http://localhost:8080/",
            "secret",
            "kb-1",
            client_factory=lambda config: WeKnoraClient(config, transport=_transport()),
        )
    )
    assert probe.ok is True
    assert probe.reachable is True
    assert probe.credentials_ok is True
    assert probe.knowledge_base_found is True
    assert probe.knowledge_base_name == "Research"


def test_probe_rejects_invisible_knowledge_base() -> None:
    probe = asyncio.run(
        probe_weknora(
            "http://localhost:8080",
            "secret",
            "missing",
            client_factory=lambda config: WeKnoraClient(config, transport=_transport()),
        )
    )
    assert probe.ok is False
    assert probe.knowledge_base_found is False
    assert "not visible" in (probe.error or "")


def test_probe_rejects_cloud_metadata_targets() -> None:
    probe = asyncio.run(
        probe_weknora(
            "http://169.254.169.254",
            "secret",
            "kb-1",
            client_factory=lambda _config: pytest.fail("unsafe probe opened a client"),
        )
    )
    assert probe.ok is False
    assert "Unsafe WeKnora server URL" in (probe.error or "")


def _kb_base(tmp_path: Path, entry: dict) -> str:
    (tmp_path / "kb_config.json").write_text(
        json.dumps({"knowledge_bases": {"remote": entry}}), encoding="utf-8"
    )
    return str(tmp_path)


def test_pipeline_returns_chunks_and_sources(tmp_path: Path) -> None:
    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        },
    )
    pipeline = WeKnoraPipeline(
        base,
        client_factory=lambda config: WeKnoraClient(config, transport=_transport()),
    )
    result = asyncio.run(pipeline.search("what is AI?", "remote"))
    assert result["provider"] == "weknora"
    assert result["content"] == "Context one\n\n---\n\nContext two"
    assert result["sources"][0]["knowledge_title"] == "Guide"


def test_pipeline_uses_native_wiki_for_wiki_only_kb(tmp_path: Path) -> None:
    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
            "weknora_capabilities": {
                "vector": False,
                "keyword": False,
                "wiki": True,
                "graph": False,
                "faq": False,
            },
        },
    )
    result = asyncio.run(
        WeKnoraPipeline(
            base,
            client_factory=lambda config: WeKnoraClient(config, transport=_wiki_transport()),
        ).search("what is AI?", "remote")
    )

    assert result["provider"] == "weknora"
    assert result["weknora_capabilities"]["wiki"] is True
    assert "# Artificial intelligence" in result["content"]
    assert result["sources"] == [
        {
            "id": "page-1",
            "chunk_id": "wiki:page-1",
            "title": "Artificial intelligence",
            "content": "# Artificial intelligence\n\nAI builds intelligent systems.",
            "source": "WeKnora Wiki / Artificial intelligence",
            "knowledge_base_id": "kb-1",
            "slug": "concept/artificial-intelligence",
            "page_type": "concept",
            "summary": "A field of computer science.",
            "aliases": [],
            "source_refs": ["doc-1|AI Guide"],
            "chunk_refs": ["chunk-1"],
            "weknora_source_type": "wiki_page",
        }
    ]


def test_pipeline_falls_back_from_natural_language_to_wiki_title(tmp_path: Path) -> None:
    """Wiki v0.8.2 treats the query as one POSIX expression, not tokenized text.

    The adapter must own that provider-specific mismatch so every caller gets
    the same title fallback instead of relying on an LLM to guess a short query.
    """

    class TitleOnlyClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def get_knowledge_base(self) -> dict:
            return {
                "id": "kb-1",
                "capabilities": {
                    "vector": False,
                    "keyword": False,
                    "wiki": True,
                    "graph": False,
                    "faq": False,
                },
            }

        async def search_wiki(self, query: str, *, limit: int) -> list[dict]:
            self.queries.append(query)
            if query != "秋天的怀念":
                return []
            return [
                {
                    "id": "page-autumn",
                    "knowledge_base_id": "kb-1",
                    "slug": "秋天的怀念",
                    "title": "《秋天的怀念》",
                    "summary": "史铁生回忆母亲的散文。",
                }
            ]

        async def get_wiki_page(self, slug: str) -> dict:
            assert slug == "秋天的怀念"
            return {
                "id": "page-autumn",
                "knowledge_base_id": "kb-1",
                "slug": slug,
                "title": "《秋天的怀念》",
                "summary": "史铁生回忆母亲的散文。",
                "content": "文章记述作者瘫痪后与母亲相处的最后时光。",
            }

    client = TitleOnlyClient()
    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        },
    )

    result = asyncio.run(
        WeKnoraPipeline(base, client_factory=lambda _config: client).search(
            "请仅根据 mida 知识库回答：秋天的怀念主要讲了什么？", "remote"
        )
    )

    assert client.queries == [
        "请仅根据 mida 知识库回答：秋天的怀念主要讲了什么？",
        "秋天的怀念",
    ]
    assert result["sources"][0]["slug"] == "秋天的怀念"
    assert "相处的最后时光" in result["content"]
    assert result["weknora_wiki_queries"] == client.queries


def test_pipeline_exposes_search_then_read_wiki_interface(tmp_path: Path) -> None:
    """Search stays lightweight; full page hydration is an explicit second step."""

    class NavigatingClient:
        def __init__(self) -> None:
            self.read_slugs: list[str] = []

        async def get_knowledge_base(self) -> dict:
            return {
                "id": "kb-1",
                "capabilities": {"wiki": True, "vector": False, "keyword": False},
            }

        async def search_wiki(self, query: str, *, limit: int) -> list[dict]:
            assert query == "秋天的怀念|史铁生"
            assert limit == 3
            return [
                {
                    "id": "page-autumn",
                    "knowledge_base_id": "kb-1",
                    "slug": "秋天的怀念",
                    "title": "《秋天的怀念》",
                    "summary": "史铁生回忆母亲的散文。",
                    "match_snippet": "母亲提出去北海看花。",
                }
            ]

        async def get_wiki_page(self, slug: str) -> dict:
            self.read_slugs.append(slug)
            return {
                "id": "page-autumn",
                "knowledge_base_id": "kb-1",
                "slug": slug,
                "title": "《秋天的怀念》",
                "summary": "史铁生回忆母亲的散文。",
                "content": "文章记述作者瘫痪后与母亲相处的最后时光。",
                "out_links": ["史铁生", "好好儿活"],
                "source_refs": ["doc-1|秋天的怀念拓展阅读.pdf"],
            }

    client = NavigatingClient()
    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        },
    )
    pipeline = WeKnoraPipeline(base, client_factory=lambda _config: client)

    hits = asyncio.run(
        pipeline.search_wiki_pages("秋天的怀念|史铁生", "remote", limit=3, regex=True)
    )
    assert client.read_slugs == []
    assert hits["pages"][0]["slug"] == "秋天的怀念"
    assert hits["pages"][0]["match_snippet"] == "母亲提出去北海看花。"

    page = asyncio.run(pipeline.read_wiki_pages(["秋天的怀念"], "remote"))
    assert client.read_slugs == ["秋天的怀念"]
    assert page["sources"][0]["out_links"] == ["史铁生", "好好儿活"]
    assert page["sources"][0]["source_refs"] == ["doc-1|秋天的怀念拓展阅读.pdf"]
    assert "相处的最后时光" in page["content"]


def test_pipeline_refreshes_saved_capabilities_from_weknora(tmp_path: Path) -> None:
    wiki_transport = _wiki_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/knowledge-bases":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "kb-1",
                            "name": "Research",
                            "type": "document",
                            "capabilities": {
                                "vector": False,
                                "keyword": False,
                                "wiki": True,
                                "graph": False,
                                "faq": False,
                            },
                        }
                    ],
                },
            )
        return wiki_transport.handle_request(request)

    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
            "weknora_capabilities": {
                "vector": True,
                "keyword": True,
                "wiki": False,
                "graph": False,
                "faq": False,
            },
        },
    )
    result = asyncio.run(
        WeKnoraPipeline(
            base,
            client_factory=lambda config: WeKnoraClient(
                config, transport=httpx.MockTransport(handler)
            ),
        ).search("what is AI?", "remote")
    )

    assert result["weknora_capabilities"]["wiki"] is True
    assert result["sources"][0]["weknora_source_type"] == "wiki_page"


def test_pipeline_combines_wiki_and_document_retrieval(tmp_path: Path) -> None:
    class HybridClient:
        async def get_knowledge_base(self) -> dict:
            return {
                "id": "kb-1",
                "capabilities": {
                    "vector": True,
                    "keyword": True,
                    "wiki": True,
                    "graph": False,
                    "faq": False,
                },
            }

        async def search_wiki(self, _query: str, *, limit: int) -> list[dict]:
            assert limit == 5
            return [{"id": "page-1", "slug": "concept/ai", "title": "AI"}]

        async def get_wiki_page(self, _slug: str) -> dict:
            return {
                "id": "page-1",
                "knowledge_base_id": "kb-1",
                "slug": "concept/ai",
                "title": "AI",
                "content": "Wiki explanation",
            }

        async def search(self, _query: str) -> list[dict]:
            return [{"id": "chunk-1", "content": "Source document", "title": "Manual"}]

    base = _kb_base(
        tmp_path,
        {
            "type": WEKNORA_KB_TYPE,
            "rag_provider": "weknora",
            "server_url": "http://localhost:8080",
            "api_key": "secret",
            "knowledge_base_id": "kb-1",
        },
    )
    result = asyncio.run(
        WeKnoraPipeline(base, client_factory=lambda _config: HybridClient()).search(
            "what is AI?", "remote"
        )
    )

    assert [source["weknora_source_type"] for source in result["sources"]] == [
        "wiki_page",
        "document_chunk",
    ]
    assert "Wiki explanation" in result["content"]
    assert "Source document" in result["content"]


def test_pipeline_reports_not_configured_and_retrieval_errors(tmp_path: Path) -> None:
    pipeline = WeKnoraPipeline(
        _kb_base(tmp_path, {"type": WEKNORA_KB_TYPE, "rag_provider": "weknora"}),
        client_factory=lambda config: WeKnoraClient(config, transport=_transport()),
    )
    result = asyncio.run(pipeline.search("q", "remote"))
    assert result["error_type"] == "not_configured"

    configured = WeKnoraPipeline(
        _kb_base(
            tmp_path,
            {
                "type": WEKNORA_KB_TYPE,
                "rag_provider": "weknora",
                "server_url": "http://localhost:8080",
                "api_key": "secret",
                "knowledge_base_id": "missing",
            },
        ),
        client_factory=lambda config: WeKnoraClient(
            config, transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
        ),
    )
    failed = asyncio.run(configured.search("q", "remote"))
    assert failed["error_type"] == "retrieval_error"


def test_pipeline_refuses_local_indexing(tmp_path: Path) -> None:
    pipeline = WeKnoraPipeline(str(tmp_path))
    with pytest.raises(RuntimeError, match="managed in WeKnora"):
        asyncio.run(pipeline.initialize("remote", []))


def test_factory_routes_weknora() -> None:
    assert normalize_provider_name("WeKnora") == "weknora"
    assert type(get_pipeline("weknora", kb_base_dir="/tmp/kbs")).__name__ == "WeKnoraPipeline"
    assert any(item["id"] == "weknora" for item in list_pipelines())


def test_manager_pointer_hides_api_key(tmp_path: Path) -> None:
    manager = KnowledgeBaseManager(base_dir=str(tmp_path))
    entry = manager.register_weknora_kb(
        "Remote",
        "http://localhost:8080/",
        "secret",
        "kb-1",
        knowledge_base_type="document",
        capabilities={"wiki": True, "vector": False, "keyword": False},
    )
    assert WEKNORA_KB_TYPE in CONNECTED_KB_TYPES
    assert entry["server_url"] == "http://localhost:8080"
    assert not (tmp_path / "Remote").exists()

    metadata = manager.get_metadata("Remote")
    assert metadata["knowledge_base_id"] == "kb-1"
    assert metadata["weknora_capabilities"]["wiki"] is True
    assert "api_key" not in metadata
    assert "secret" not in str(metadata)
