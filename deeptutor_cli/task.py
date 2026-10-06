"""CLI commands for the active workspace's task board."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from pydantic import ValidationError
from rich.table import Table
import typer

from deeptutor.services.task_board import CreateCard, TaskCard, UpdateCard, get_task_board_store

from .common import console

_STATUSES = {"todo", "doing", "done"}


def _fail(message: str) -> NoReturn:
    console.print(f"[red]{message}[/]")
    raise typer.Exit(code=1)


def _card(card_id: str) -> TaskCard:
    try:
        return get_task_board_store().get(card_id)
    except KeyError:
        _fail(f"Task not found: {card_id}")


def _print_card(card: TaskCard, *, fmt: str = "rich") -> None:
    if fmt == "json":
        console.print(json.dumps(card.model_dump(), ensure_ascii=False, indent=2))
        return
    console.print(f"[bold]{card.title}[/] [cyan]{card.id}[/]")
    console.print(f"Status: {card.status}")
    console.print(f"Archived: {'yes' if card.archived else 'no'}")
    if card.note:
        console.print(f"Note: {card.note}")
    console.print(f"Created: {card.created_at}")
    console.print(f"Updated: {card.updated_at}")


def _print_cards(cards: list[TaskCard], *, fmt: str = "rich") -> None:
    if fmt == "json":
        payload = [card.model_dump() for card in cards]
        console.print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not cards:
        console.print("[dim]No matching tasks.[/]")
        return
    table = Table(title="Task Board")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Note", style="dim")
    for card in cards:
        status = f"{card.status}{' · archived' if card.archived else ''}"
        table.add_row(card.id, status, card.title, card.note)
    console.print(table)


def _update(card_id: str, changes: dict[str, Any], *, verb: str = "Updated") -> TaskCard:
    try:
        payload = UpdateCard(**changes)
        store = get_task_board_store()
        store.update(card_id, payload)
        card = store.get(card_id)
    except ValidationError as exc:
        _fail(f"Invalid task update: {exc}")
    except KeyError:
        _fail(f"Task not found: {card_id}")
    console.print(f"[green]{verb} task[/] {card.id}: {card.title}")
    return card


def register(app: typer.Typer) -> None:
    @app.command("add")
    def add_task(
        title: str = typer.Argument(..., help="Task title."),
        note: str = typer.Option("", "--note", "-n", help="Optional task note."),
    ) -> None:
        """Add a task to the To do column."""
        try:
            card = get_task_board_store().add(CreateCard(title=title, note=note))
        except ValidationError as exc:
            _fail(f"Invalid task: {exc}")
        console.print(f"[green]Created task[/] {card.id}: {card.title}")

    @app.command("list")
    def list_tasks(
        include_archived: bool = typer.Option(
            False, "--all", help="Include archived tasks."
        ),
        status: str | None = typer.Option(
            None, "--status", help="Filter by todo, doing, or done."
        ),
        fmt: str = typer.Option("rich", "--format", "-f", help="Output: rich | json."),
    ) -> None:
        """List tasks in the active workspace."""
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in _STATUSES:
            _fail(f"Invalid status: {status}")
        cards = [
            card
            for card in get_task_board_store().read().cards
            if (include_archived or not card.archived)
            and (not normalized_status or card.status == normalized_status)
        ]
        _print_cards(cards, fmt=fmt)

    @app.command("show")
    def show_task(
        card_id: str = typer.Argument(..., help="Task id."),
        fmt: str = typer.Option("rich", "--format", "-f", help="Output: rich | json."),
    ) -> None:
        """Show one task, including archived tasks."""
        _print_card(_card(card_id), fmt=fmt)

    @app.command("update")
    def update_task(
        card_id: str = typer.Argument(..., help="Task id."),
        title: str | None = typer.Option(None, "--title", help="Replace the title."),
        note: str | None = typer.Option(
            None, "--note", "-n", help="Replace the note; pass an empty value to clear it."
        ),
        status: str | None = typer.Option(
            None, "--status", help="Set todo, doing, or done."
        ),
    ) -> None:
        """Modify a task's title, note, or status."""
        changes = {
            key: value
            for key, value in {"title": title, "note": note, "status": status}.items()
            if value is not None
        }
        if not changes:
            _fail("Provide at least one of --title, --note, or --status.")
        _update(card_id, changes)

    @app.command("complete")
    def complete_task(card_id: str = typer.Argument(..., help="Task id.")) -> None:
        """Mark a task as done."""
        _update(card_id, {"status": "done"}, verb="Completed")

    @app.command("archive")
    def archive_task(card_id: str = typer.Argument(..., help="Task id.")) -> None:
        """Archive a task without deleting it."""
        _update(card_id, {"archived": True}, verb="Archived")

    @app.command("restore")
    def restore_task(card_id: str = typer.Argument(..., help="Task id.")) -> None:
        """Restore an archived task to the board."""
        _update(card_id, {"archived": False}, verb="Restored")


__all__ = ["register"]
