"""Conservative objective grading; prose and qualitative work uses self-assessment."""

from __future__ import annotations

import json
import re
from fractions import Fraction


def _number(value: str) -> Fraction | None:
    value = value.strip().replace("−", "-")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:/\d+)?", value):
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def check_answer(entry: dict, answer: str) -> bool | None:
    kind = entry.get("question_type", "")
    options = entry.get("options") or json.loads(entry.get("options_json") or "{}")
    expected = str(entry.get("correct_answer") or "").strip()
    if (
        not expected
        or kind in {"qualitative", "short_answer", "essay", "free_response"}
    ):
        return None

    if not options:
        if kind in {"fill_blank", "fill_in_blank"}:
            number = _number(expected)
            return None if number is None else _number(answer) == number
        if kind == "true_false":
            booleans = {"true": True, "false": False, "正确": True, "错误": False}
            key = expected.casefold()
            if key in booleans:
                actual = answer.strip().casefold()
                return actual in booleans and booleans[actual] == booleans[key]
        return None

    def folded(text: str) -> str:
        return " ".join(text.strip().upper().split())

    def keys(value: str) -> set[str]:
        for key, text in options.items():
            if folded(value) in {
                folded(key),
                folded(text),
                folded(f"{key}. {text}"),
                folded(f"{key}) {text}"),
            }:
                return {key}
        parts = {part.strip().upper() for part in re.split(r"[,;，；\s]+", value) if part.strip()}
        if (
            kind in {"multi_choice", "multiple_select"}
            and len(parts) == 1
            and not parts <= options.keys()
        ):
            parts = set(value.strip().upper())
        return parts

    expected_keys, actual_keys = keys(expected), keys(answer)
    if not expected_keys or not expected_keys <= options.keys():
        return None  # A malformed legacy answer key must never invent a grade.
    return bool(actual_keys) and actual_keys == expected_keys
