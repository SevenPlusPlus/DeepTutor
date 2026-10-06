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
    from deeptutor.services import task_board as task_board_service

    paths = PathService(workspace_root=tmp_path / "runtime")
    paths.ensure_all_directories()
    monkeypatch.setattr(task_board_service, "get_path_service", lambda: paths)
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
    card = get_task_board_store().get(card.id)
    assert (card.title, card.note, card.status) == (
        "Review absolute values by Friday",
        "Finish mistakes 1-30",
        "doing",
    )

    completed = runner.invoke(app, ["task", "complete", card.id])
    archived = runner.invoke(app, ["task", "archive", card.id])
    assert completed.exit_code == archived.exit_code == 0
    card = get_task_board_store().get(card.id)
    assert card.status == "done"
    assert card.archived is True
    assert card.id not in runner.invoke(app, ["task", "list"]).output
    assert card.id in runner.invoke(app, ["task", "list", "--all"]).output

    restored = runner.invoke(app, ["task", "restore", card.id])
    assert restored.exit_code == 0, restored.output
    assert get_task_board_store().get(card.id).archived is False


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
