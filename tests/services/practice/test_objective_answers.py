"""Only unambiguous objective answers can supply automatic mastery evidence."""

import pytest

from deeptutor.services.practice.answers import check_answer


@pytest.mark.parametrize(
    ("expected", "answer", "correct"),
    [
        ("0.5", "1/2", True),
        ("-2", "−2", True),
        ("3/4", "0.75", True),
        ("5", "4", False),
        ("5", "I think five", False),
        ("1/2", "1/0", False),
    ],
)
def test_exact_numeric_fill_blank(expected, answer, correct):
    assert (
        check_answer({"question_type": "fill_blank", "correct_answer": expected}, answer) is correct
    )


@pytest.mark.parametrize("expected", ["x = 2", "5 cm", "1/0", "a detailed explanation"])
def test_ambiguous_keys_require_review(expected):
    assert (
        check_answer({"question_type": "fill_blank", "correct_answer": expected}, expected) is None
    )


def test_short_answers_are_not_graded_by_numeric_coincidence():
    assert check_answer({"question_type": "short_answer", "correct_answer": "5"}, "5") is None


def test_true_false_without_options():
    entry = {"question_type": "true_false", "correct_answer": "true"}
    assert check_answer(entry, "正确") is True
    assert check_answer(entry, "false") is False
    assert check_answer(entry, "maybe") is False
