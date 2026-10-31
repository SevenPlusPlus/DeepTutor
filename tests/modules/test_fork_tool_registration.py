"""Fork tools follow the upstream module catalog and dependency gates."""

from deeptutor.modules import catalog
from deeptutor.runtime.registry.tool_registry import ToolRegistry


def test_core_keeps_task_tools_without_importing_partners(monkeypatch):
    from deeptutor.tools import task_board

    monkeypatch.setattr(catalog, "installed", lambda name: name == "core")
    registry = ToolRegistry()
    registry.load_builtins()
    assert registry.get("task_board") is not None
    assert registry.get("wiki_search") is None
    assert registry.get("wiki_read_page") is None
    assert task_board._partner_turn() is None


def test_wiki_tools_belong_to_knowledge_and_obey_its_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "state_path", lambda: tmp_path / "modules.json")
    registry = ToolRegistry()
    registry.load_builtins()
    assert registry.get("wiki_search") is not None
    assert registry.get("wiki_read_page") is not None
    assert catalog.module_for_path("deeptutor/tools/weknora_wiki.py") == "knowledge"
    catalog.state_path().write_text('{"schema_version":1,"enabled":{"knowledge":false}}')
    assert registry.get("wiki_search") is None
    assert registry.get("wiki_read_page") is None
