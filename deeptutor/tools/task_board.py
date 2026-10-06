"""LLM-callable task-board operations shared by product and Partner chat."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from deeptutor.core.tool_protocol import (
    BaseTool,
    ToolDefinition,
    ToolParameter,
    ToolPromptHints,
    ToolResult,
)
from deeptutor.services.task_board import (
    CreateCard,
    TaskBoardStore,
    TaskCard,
    UpdateCard,
    get_task_board_store,
)

_ACTIONS = ["create", "list", "get", "update", "complete", "archive", "restore"]
_STATUSES = ["todo", "doing", "done"]


class TaskBoardAccessError(PermissionError):
    """The current conversation cannot safely identify a personal board."""


def _store_for_turn() -> TaskBoardStore:
    """Resolve the board without ever writing into a Partner's synthetic scope.

    Product chat already runs in the authenticated user's selected workspace,
    so its ambient store is correct. Partner turns run inside the Partner's
    content scope; an external direct message must instead target the linked
    human's default workspace. An unlinked or group message has no actor and
    therefore cannot mutate anybody's personal board.
    """
    from deeptutor.services.partners.interaction import get_partner_turn_context

    turn = get_partner_turn_context()
    if turn is None:
        return get_task_board_store()
    if turn.actor is None:
        raise TaskBoardAccessError(
            "This chat account is not linked to a DeepTutor user. "
            "Use /link in a direct message before managing personal tasks."
        )

    from deeptutor.multi_user.paths import get_path_service_for_scope

    paths = get_path_service_for_scope(turn.actor.scope)
    return TaskBoardStore(paths.get_workspace_dir() / "task-board" / "cards.sqlite")


def _card_payload(card: TaskCard) -> dict[str, Any]:
    return card.model_dump()


def _render_card(card: TaskCard) -> str:
    flags = [card.status]
    if card.archived:
        flags.append("archived")
    note = f" — {card.note}" if card.note else ""
    return f"[{card.id}] {card.title} ({', '.join(flags)}){note}"


class TaskBoardTool(BaseTool):
    """Create, inspect and maintain cards in the learner's task board."""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_board",
            description=(
                "Create, list, inspect, edit, complete, archive, or restore tasks in "
                "the user's DeepTutor Task Board. For an existing task, list first "
                "when its card_id is not already known; never invent card ids."
            ),
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="Task-board operation to perform.",
                    enum=_ACTIONS,
                ),
                ToolParameter(
                    name="card_id",
                    type="string",
                    description="Existing task id; required except for create and list.",
                    required=False,
                ),
                ToolParameter(
                    name="title",
                    type="string",
                    description="Task title; required for create, optional for update.",
                    required=False,
                ),
                ToolParameter(
                    name="note",
                    type="string",
                    description="Optional task note. Pass an empty string to clear it.",
                    required=False,
                ),
                ToolParameter(
                    name="status",
                    type="string",
                    description="Optional status for update or list filtering.",
                    enum=_STATUSES,
                    required=False,
                ),
                ToolParameter(
                    name="include_archived",
                    type="boolean",
                    description="List archived tasks too (default false).",
                    required=False,
                ),
            ],
        )

    def get_prompt_hints(self, language: str = "en") -> ToolPromptHints:
        from deeptutor.tools.prompting import load_prompt_hints

        return load_prompt_hints(self.name, language=language)

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return ToolResult(
                content=f"Error: action must be one of {', '.join(_ACTIONS)}.", success=False
            )

        try:
            store = _store_for_turn()
        except TaskBoardAccessError as exc:
            return ToolResult(content=f"Task board unavailable: {exc}", success=False)

        if action == "list":
            status = str(kwargs.get("status") or "").strip().lower()
            if status and status not in _STATUSES:
                return ToolResult(content=f"Error: invalid task status {status!r}.", success=False)
            include_archived = bool(kwargs.get("include_archived", False))
            cards = [
                card
                for card in store.read().cards
                if (include_archived or not card.archived)
                and (not status or card.status == status)
            ]
            if not cards:
                return ToolResult(
                    content="No matching tasks.",
                    metadata={"action": action, "cards": [], "count": 0},
                )
            return ToolResult(
                content="\n".join(_render_card(card) for card in cards),
                metadata={
                    "action": action,
                    "cards": [_card_payload(card) for card in cards],
                    "count": len(cards),
                },
            )

        if action == "create":
            try:
                card = store.add(
                    CreateCard(
                        title=kwargs.get("title", ""),
                        note=kwargs.get("note", ""),
                    )
                )
            except ValidationError as exc:
                return ToolResult(content=f"Could not create task: {exc}", success=False)
            return ToolResult(
                content=f"Created task: {_render_card(card)}",
                metadata={"action": action, "card": _card_payload(card)},
            )

        card_id = str(kwargs.get("card_id") or "").strip()
        if not card_id:
            return ToolResult(
                content=f"Error: card_id is required for action={action!r}.", success=False
            )

        if action == "get":
            try:
                card = store.get(card_id)
            except KeyError:
                return ToolResult(content=f"Task not found: {card_id}", success=False)
            return ToolResult(
                content=_render_card(card),
                metadata={"action": action, "card": _card_payload(card)},
            )

        if action == "update":
            changes = {
                key: kwargs[key]
                for key in ("title", "note", "status")
                if key in kwargs and kwargs[key] is not None
            }
            if not changes:
                return ToolResult(
                    content="Error: update requires title, note, or status.", success=False
                )
            try:
                payload = UpdateCard(**changes)
            except ValidationError as exc:
                return ToolResult(content=f"Could not update task: {exc}", success=False)
        elif action == "complete":
            payload = UpdateCard(status="done")
        elif action == "archive":
            payload = UpdateCard(archived=True)
        else:  # restore
            payload = UpdateCard(archived=False)

        try:
            store.update(card_id, payload)
            card = store.get(card_id)
        except KeyError:
            return ToolResult(content=f"Task not found: {card_id}", success=False)
        return ToolResult(
            content=f"Updated task: {_render_card(card)}",
            metadata={"action": action, "card": _card_payload(card)},
        )


__all__ = ["TaskBoardAccessError", "TaskBoardTool"]
