"""Deterministic question normalization shared by every import source.

File parsing, OCR and model calls deliberately live elsewhere.  This module is
the single seam that turns an untrusted question-shaped mapping into the
canonical payload accepted by :class:`PracticeStore`.
"""

from __future__ import annotations

import hashlib
import json
import re

MAX_CELL = 20_000
ALIASES = {
    "question": ("question", "题目", "题干"),
    "question_type": ("question_type", "type", "题型"),
    "correct_answer": ("correct_answer", "answer", "答案", "正确答案"),
    "explanation": ("explanation", "解析"),
    "difficulty": ("difficulty", "难度"),
    "tags": ("tags", "标签", "分类"),
    "user_answer": ("user_answer", "我的答案", "作答"),
    "options": ("options", "选项"),
}
TYPES = {
    "single_choice": "single_choice",
    "multiple_choice": "single_choice",
    "mcq": "single_choice",
    "单选": "single_choice",
    "单选题": "single_choice",
    "multi_choice": "multi_choice",
    "多选": "multi_choice",
    "多选题": "multi_choice",
    "true_false": "true_false",
    "判断": "true_false",
    "判断题": "true_false",
    "fill_blank": "fill_blank",
    "填空": "fill_blank",
    "填空题": "fill_blank",
    "short_answer": "short_answer",
    "简答": "short_answer",
    "简答题": "short_answer",
    "essay": "short_answer",
    "free_response": "short_answer",
}


def cell_text(value: object) -> str:
    """Return a bounded, stripped cell value."""

    text = "" if value is None else str(value).strip()
    if len(text) > MAX_CELL:
        raise ValueError("A cell exceeds 20,000 characters")
    return text


def normalize_question(raw: dict, *, require_answer: bool = True) -> dict:
    """Normalize one question or raise a stable validation error.

    The output is safe to stage for import and contains a deterministic
    ``question_id`` derived from the fields that define duplicate identity.
    Structured imports keep the default answer requirement; recognition may
    opt out so a source question with no answer can remain explicitly ungraded.
    """

    row = {str(key).strip().lower(): value for key, value in raw.items()}
    fields = {key: next((row[a] for a in names if a in row), "") for key, names in ALIASES.items()}
    question, answer = cell_text(fields["question"]), cell_text(fields["correct_answer"])
    if not question or (require_answer and not answer):
        raise ValueError("Question and correct answer are required")
    options = fields["options"] or {}
    if isinstance(options, str):
        options = json.loads(options)
    if isinstance(options, list):
        options = {chr(65 + i): value for i, value in enumerate(options)}
    if not isinstance(options, dict) or len(options) > 10:
        raise ValueError("Options must contain at most 10 named choices")
    options = {
        str(key).strip().upper(): cell_text(value)
        for key, value in options.items()
        if cell_text(value)
    }
    for letter in "ABCDEFGHIJ":
        value = row.get(letter.lower(), row.get(f"选项{letter.lower()}", ""))
        if cell_text(value):
            options[letter] = cell_text(value)
    raw_type = cell_text(fields["question_type"]).lower()
    kind = TYPES.get(raw_type) if raw_type else ("single_choice" if options else "short_answer")
    if kind is None:
        raise ValueError("Unknown question type")
    if kind == "true_false":
        truths = {"true", "正确", "对", "是", "t", "1"}
        falses = {"false", "错误", "错", "否", "f", "0"}
        if answer and answer.lower() not in truths | falses:
            raise ValueError("A true/false answer must be true or false")
        options = {"T": "True", "F": "False"}
        if answer:
            answer = "T" if answer.lower() in truths else "F"
    if kind in {"single_choice", "multi_choice"}:
        if len(options) < 2:
            raise ValueError("Choice questions need at least two options")
        if answer and answer in options.values():
            answer = next(key for key, text in options.items() if text == answer)
        keys = [key.strip().upper() for key in re.split(r"[,;，；\s]+", answer) if key.strip()]
        if kind == "multi_choice" and len(keys) == 1 and keys[0] not in options:
            keys = list(keys[0])
        if answer and (
            any(key not in options for key in keys)
            or (kind == "single_choice" and len(keys) != 1)
        ):
            raise ValueError("The answer must name existing option keys (multi-choice: A,C)")
        if answer:
            answer = ",".join(sorted(set(keys)))
    tags_value = fields["tags"]
    tags = tags_value if isinstance(tags_value, list) else re.split(r"[,;，；]", cell_text(tags_value))
    tags = list(dict.fromkeys(cell_text(tag) for tag in tags if cell_text(tag)))
    if len(tags) > 20 or any(len(tag) > 100 for tag in tags):
        raise ValueError("Use at most 20 tags, each up to 100 characters")
    result = dict(
        question=question,
        question_type=kind,
        options=options,
        correct_answer=answer,
        explanation=cell_text(fields["explanation"]),
        difficulty=cell_text(fields["difficulty"]),
        user_answer=cell_text(fields["user_answer"]),
        tags=tags,
    )
    identity = {
        key: result[key] for key in ("question", "question_type", "options", "correct_answer")
    }
    result["question_id"] = (
        "import:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    )
    return result


__all__ = ["ALIASES", "MAX_CELL", "TYPES", "cell_text", "normalize_question"]
