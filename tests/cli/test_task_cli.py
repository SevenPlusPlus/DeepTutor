"""CLI coverage for ``deeptutor task`` task-board operations."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptutor.services.path_service import PathService
from deeptutor.services.task_board import get_task_board_store
from deeptutor_cli.main import app

runner = CliRunner()


@pytest.fixture
def task_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PathService:
    from deeptutor.multi_user import paths as user_paths

    paths = PathService(workspace_root=tmp_path / "runtime")
    paths.ensure_all_directories()
    monkeypatch.setattr(user_paths, "get_account_path_service", lambda: paths)
    monkeypatch.setattr(PathService, "_instance", paths)
    return paths


def test_task_cli_full_lifecycle(task_paths: PathService) -> None:
    created = runner.invoke(
        app,
        ["task", "add", "Review absolute values", "--note", "Finish mistakes 1-20"],
    )
    assert created.exit_code == 0, created.output
    card = get_task_board_store().read().cards[0]
    assert card.title == "Review absolute values"
    assert card.note == "Finish mistakes 1-20"
    assert card.status == "todo"

    listed = runner.invoke(app, ["task", "list"])
    shown = runner.invoke(app, ["task", "show", card.id])
    assert listed.exit_code == shown.exit_code == 0
    assert card.id in listed.output
    assert "Review absolute values" in shown.output

    updated = runner.invoke(
        app,
        [
            "task",
            "update",
            card.id,
            "--title",
            "Review absolute values by Friday",
            "--note",
            "Finish mistakes 1-30",
            "--status",
            "doing",
        ],
    )
    assert updated.exit_code == 0, updated.output
    card = get_task_board_store().get(card.id, workspace_id="")
    assert (card.title, card.note, card.status) == (
        "Review absolute values by Friday",
        "Finish mistakes 1-30",
        "doing",
    )

    completed = runner.invoke(app, ["task", "complete", card.id])
    archived = runner.invoke(app, ["task", "archive", card.id])
    assert completed.exit_code == archived.exit_code == 0
    card = get_task_board_store().get(card.id, workspace_id="")
    assert card.status == "done"
    assert card.archived is True
    assert card.id not in runner.invoke(app, ["task", "list"]).output
    assert card.id in runner.invoke(app, ["task", "list", "--all"]).output

    restored = runner.invoke(app, ["task", "restore", card.id])
    assert restored.exit_code == 0, restored.output
    assert get_task_board_store().get(card.id, workspace_id="").archived is False


def test_task_cli_rejects_missing_and_invalid_updates(task_paths: PathService) -> None:
    missing = runner.invoke(app, ["task", "complete", "missing"])
    empty = runner.invoke(app, ["task", "update", "missing"])
    invalid = runner.invoke(app, ["task", "list", "--status", "blocked"])

    assert missing.exit_code == 1
    assert "Task not found" in missing.output
    assert empty.exit_code == 1
    assert "Provide at least one" in empty.output
    assert invalid.exit_code == 1
    assert "Invalid status" in invalid.output


def test_task_cli_respects_the_active_workspace(task_paths: PathService, monkeypatch) -> None:
    from deeptutor.services.task_board import CreateCard
    from deeptutor_cli import task

    store = get_task_board_store()
    other = store.add(CreateCard(title="Other workspace task"), workspace_id="other")
    monkeypatch.setattr(task, "current_workspace_id", lambda: "study")

    created = runner.invoke(app, ["task", "add", "Study task"])
    assert created.exit_code == 0, created.output
    own = next(card for card in store.read().cards if card.title == "Study task")
    assert own.workspace_id == "study"
    listed = runner.invoke(app, ["task", "list"])
    assert own.id in listed.output
    assert other.id not in listed.output
    for arguments in (["show", other.id], ["complete", other.id]):
        result = runner.invoke(app, ["task", *arguments])
        assert result.exit_code == 1
        assert "Task not found" in result.output
    assert store.get(other.id, workspace_id="other").status == "todo"
