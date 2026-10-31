"""Task Board built-in tool behavior and Partner identity isolation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.services.task_board import CreateCard, TaskBoardStore
from deeptutor.tools.builtin import BUILTIN_TOOL_NAMES, CONFIGURABLE_BUILTIN_TOOL_NAMES
from deeptutor.tools.task_board import TaskBoardTool


@pytest.mark.asyncio
async def test_task_board_tool_full_lifecycle(tmp_path, monkeypatch) -> None:
    from deeptutor.tools import task_board as task_board_tool

    store = TaskBoardStore(tmp_path / "board" / "cards.sqlite")
    monkeypatch.setattr(task_board_tool, "_store_for_turn", lambda: store)
    tool = TaskBoardTool()

    created = await tool.execute(
        action="create", title="Review absolute values", note="Finish mistakes 1-20"
    )
    assert created.success is True
    card_id = created.metadata["card"]["id"]

    listed = await tool.execute(action="list")
    assert listed.metadata["count"] == 1
    assert card_id in listed.content

    updated = await tool.execute(
        action="update", card_id=card_id, title="Review by Friday", status="doing"
    )
    completed = await tool.execute(action="complete", card_id=card_id)
    archived = await tool.execute(action="archive", card_id=card_id)
    assert updated.success and completed.success and archived.success
    assert archived.metadata["card"]["status"] == "done"
    assert archived.metadata["card"]["archived"] is True
    assert (await tool.execute(action="list")).metadata["count"] == 0
    assert (await tool.execute(action="list", include_archived=True)).metadata["count"] == 1

    restored = await tool.execute(action="restore", card_id=card_id)
    shown = await tool.execute(action="get", card_id=card_id)
    assert restored.metadata["card"]["archived"] is False
    assert shown.metadata["card"]["title"] == "Review by Friday"


@pytest.mark.asyncio
async def test_task_board_tool_rejects_unlinked_partner_chat(tmp_path, monkeypatch) -> None:
    from deeptutor.services.partners import interaction

    monkeypatch.setattr(
        interaction,
        "get_partner_turn_context",
        lambda: SimpleNamespace(actor=None),
    )

    result = await TaskBoardTool().execute(action="create", title="Private task")

    assert result.success is False
    assert "/link" in result.content
    assert not list(tmp_path.rglob("cards.sqlite"))


@pytest.mark.asyncio
async def test_task_board_tool_writes_linked_partners_task_to_the_human_scope(
    tmp_path, monkeypatch
) -> None:
    from deeptutor.services.partners import interaction

    actor_root = (tmp_path / "users" / "alice").resolve()
    actor = CurrentUser(
        id="alice",
        username="alice",
        role="user",
        scope=UserScope(kind="user", user_id="alice", root=actor_root),
    )
    monkeypatch.setattr(
        interaction,
        "get_partner_turn_context",
        lambda: SimpleNamespace(actor=actor),
    )

    result = await TaskBoardTool().execute(action="create", title="Alice's task")

    expected = actor_root / "user" / ".runtime" / "task-board" / "tasks.sqlite"
    assert result.success is True
    assert expected.is_file()
    assert TaskBoardStore(expected).read().cards[0].title == "Alice's task"
    assert TaskBoardStore(expected).read().cards[0].workspace_id == ""
    assert not (actor_root / "user" / "workspace" / "task-board" / "cards.sqlite").exists()


def test_task_board_tool_is_registered_and_partner_configurable() -> None:
    assert "task_board" in BUILTIN_TOOL_NAMES
    assert "task_board" in CONFIGURABLE_BUILTIN_TOOL_NAMES


@pytest.mark.asyncio
async def test_task_board_tool_never_reads_or_updates_another_workspace(tmp_path, monkeypatch):
    from deeptutor.tools import task_board as task_board_tool

    store = TaskBoardStore(tmp_path / "tasks.sqlite")
    other = store.add(CreateCard(title="Other workspace"), workspace_id="other")
    monkeypatch.setattr(task_board_tool, "_store_for_turn", lambda: store)
    monkeypatch.setattr(task_board_tool, "_partner_turn", lambda: None)
    monkeypatch.setattr(task_board_tool, "current_workspace_id", lambda: "study")
    tool = TaskBoardTool()

    created = await tool.execute(action="create", title="Study workspace")
    assert created.success
    assert created.metadata["card"]["workspace_id"] == "study"
    listed = await tool.execute(action="list")
    assert listed.metadata["count"] == 1
    assert other.id not in listed.content
    for action in ("get", "update", "complete", "archive", "restore"):
        result = await tool.execute(action=action, card_id=other.id, title="Unauthorized")
        assert not result.success
        assert "Task not found" in result.content
    assert store.get(other.id, workspace_id="other").title == "Other workspace"


def test_atomic_task_creation_returns_each_callers_card(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = TaskBoardStore(tmp_path / "tasks.sqlite")
    store.create(CreateCard(title="Initial"), "study")

    def add(index):
        return store.add(CreateCard(title=f"Task {index}"), workspace_id="study")

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(add, range(12)))
    assert [card.title for card in created] == [f"Task {index}" for index in range(12)]
    assert len({card.id for card in created}) == 12
    assert store.revision() == 13
    for card in created:
        assert store.get(card.id, workspace_id="study") == card
        with pytest.raises(KeyError):
            store.get(card.id, workspace_id="other")
