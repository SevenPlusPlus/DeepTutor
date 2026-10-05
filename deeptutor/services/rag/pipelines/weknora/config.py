"""Per-KB connection configuration for Tencent WeKnora."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class WeKnoraNotConfiguredError(RuntimeError):
    """Raised when a KB lacks the fields needed to reach WeKnora."""


@dataclass(frozen=True)
class WeKnoraConfig:
    base_url: str
    api_key: str
    knowledge_base_id: str
    capabilities: dict[str, bool] | None = None


def capabilities_from_knowledge_base(item: dict[str, Any]) -> dict[str, bool]:
    """Return the retrieval features advertised by a WeKnora KB.

    Current WeKnora releases expose a computed ``capabilities`` object.  The
    indexing strategy is kept as a compatibility fallback for older servers,
    while ``type: wiki`` covers the first Wiki schema which predated both.
    An empty result means the server did not expose enough information; callers
    must preserve the legacy ``knowledge-search`` behaviour in that case.
    """

    advertised = item.get("capabilities")
    strategy = item.get("indexing_strategy")
    advertised = advertised if isinstance(advertised, dict) else {}
    strategy = strategy if isinstance(strategy, dict) else {}

    legacy_type = str(item.get("type") or "").strip().lower()
    signals = bool(advertised or strategy or legacy_type in {"wiki", "faq"})
    if not signals:
        return {}

    def enabled(name: str) -> bool:
        value = advertised.get(name)
        if isinstance(value, bool):
            return value
        value = strategy.get(f"{name}_enabled")
        if isinstance(value, bool):
            return value
        return name == "wiki" and legacy_type == "wiki"

    return {
        "vector": enabled("vector"),
        "keyword": enabled("keyword"),
        "wiki": enabled("wiki"),
        "graph": enabled("graph"),
        "faq": bool(advertised.get("faq"))
        or legacy_type == "faq",
    }


def normalize_base_url(url: str | None) -> str:
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return normalized


def config_from_entry(entry: dict[str, Any]) -> WeKnoraConfig:
    base_url = normalize_base_url(entry.get("server_url"))
    api_key = str(entry.get("api_key") or "").strip()
    knowledge_base_id = str(entry.get("knowledge_base_id") or "").strip()
    if not base_url or not knowledge_base_id:
        raise WeKnoraNotConfiguredError(
            "This knowledge base is not connected to WeKnora "
            "(server URL and knowledge base ID are required)."
        )
    if not api_key:
        raise WeKnoraNotConfiguredError("A WeKnora API key is required for retrieval.")
    raw_capabilities = entry.get("weknora_capabilities")
    capabilities = (
        {
            str(key): bool(value)
            for key, value in raw_capabilities.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        if isinstance(raw_capabilities, dict)
        else None
    )
    return WeKnoraConfig(
        base_url=base_url,
        api_key=api_key,
        knowledge_base_id=knowledge_base_id,
        capabilities=capabilities,
    )


__all__ = [
    "WeKnoraConfig",
    "WeKnoraNotConfiguredError",
    "capabilities_from_knowledge_base",
    "config_from_entry",
    "normalize_base_url",
]
