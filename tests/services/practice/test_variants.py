from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from deeptutor.services.practice.storage import PracticeStore, ReviewConflict
from deeptutor.services.practice.variants import LLMVariantGenerator, VariantService
from deeptutor.services.session.sqlite_store import SQLiteSessionStore


def question(**changes):
    return {
        "question": "Solve 2x + 3 = 11. What is x?",
        "question_type": "single_choice",
        "options": {"A": "3", "B": "4"},
        "correct_answer": "B",
        "explanation": "Subtract 3 from both sides, then divide by 2: x = 4.",
        "difficulty": "medium",
        "knowledge_point": "linear-equations",
        "target_skill": "linear-equations",
        "introduces_new_skill": False,
        "subject": "math",
        **changes,
    }


class Generator:
    def __init__(self, payload=None):
        self.calls = 0
        self.payload = payload if payload is not None else question()

    async def generate(self, parent, kind):
        self.calls += 1
        return self.payload


@pytest.fixture
def bank(tmp_path):
    store = SQLiteSessionStore(tmp_path / "variants.db")

    async def seed():
        await store.ensure_notebook_session("lesson", "Algebra")
        await store.upsert_notebook_entries(
            "lesson",
            [
                {
                    "question_id": "original",
                    "question": "Solve 3x - 5 = 10. What is x?",
                    "question_type": "single_choice",
                    "options": {"A": "4", "B": "5"},
                    "correct_answer": "B",
                    "user_answer": "A",
                    "is_correct": False,
                    "knowledge_point_id": "linear-equations",
                    "course_id": "course-one",
                    "recognition_meta": {"subject": "math"},
                }
            ],
        )
        return await store.find_notebook_entry("lesson", "original")

    parent = asyncio.run(seed())
    with PracticeStore(store.db_path).connect() as conn:
        conn.execute(
            "UPDATE notebook_entries SET course_id='course-one',recognition_meta_json=? WHERE id=?",
            ('{"subject":"math"}', parent["id"]),
        )
    parent = asyncio.run(store.get_notebook_entry(parent["id"]))
    generator = Generator()
    service = VariantService(store.db_path, generator)
    return store, service, generator, parent


def generate(service, parent, request_id="generate_variant_0001", kind="similar"):
    return asyncio.run(service.generate(parent["id"], request_id, kind))


def approve(service, parent, draft, **changes):
    return service.confirm(
        parent["id"],
        draft["draft_id"],
        draft["version"],
        {**question(), **changes},
        confirmed=True,
    )


def test_generation_is_pending_review_and_request_id_is_idempotent(bank):
    store, service, generator, parent = bank
    first = generate(service, parent)
    second = generate(service, parent)
    assert first == second
    assert first["status"] == "ready" and first["parent_entry_id"] == parent["id"]
    assert generator.calls == 1
    assert asyncio.run(store.list_notebook_entries())["total"] == 1
    with service.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_variants").fetchone()[0] == 0
        exposure = conn.execute("SELECT * FROM practice_exposures").fetchone()
        assert (
            exposure["entry_id"] == parent["id"]
        )  # Closing an unapproved draft cannot erase answer exposure.


def test_approval_creates_one_ungraded_child_and_records_answer_exposure(bank):
    store, service, _, parent = bank
    before = PracticeStore(store.db_path).state(parent["id"])
    draft = generate(service, parent)
    child_id = approve(service, parent, draft)
    assert approve(service, parent, draft) == child_id
    child = asyncio.run(store.get_notebook_entry(child_id))
    assert child["result"] == "ungraded" and child["user_answer"] == ""
    assert child["course_id"] == "course-one"
    assert child["answer_origin"] == "human_reviewed"
    assert child["question"] == question()["question"]
    assert PracticeStore(store.db_path).state(parent["id"]) == before
    assert child_id not in PracticeStore(store.db_path).queue(
        "UTC", now=child["created_at"] + 3 * 86400
    )
    with service.connect() as conn:
        relation = conn.execute(
            "SELECT * FROM practice_variants WHERE entry_id=?", (child_id,)
        ).fetchone()
        exposure = conn.execute(
            "SELECT * FROM practice_exposures WHERE entry_id=?", (child_id,)
        ).fetchone()
        assert relation["parent_entry_id"] == parent["id"]
        assert relation["validation_status"] == "approved"
        assert exposure["kind"] == "answer" and exposure["target_entry_id"] == parent["id"]
    assert generate(service, parent)["entry_id"] == child_id


def test_edited_approved_payload_is_the_saved_version_not_model_output(bank):
    store, service, _, parent = bank
    draft = generate(service, parent)
    changed = question(question="Solve 2x + 1 = 9. What is x?", explanation="2x = 8, so x = 4.")
    child_id = service.confirm(
        parent["id"], draft["draft_id"], draft["version"], changed, confirmed=True
    )
    child = asyncio.run(store.get_notebook_entry(child_id))
    assert child["question"] == changed["question"]
    assert child["explanation"] == changed["explanation"]
    with pytest.raises(ReviewConflict, match="different question details"):
        approve(service, parent, draft)


def test_confirmation_is_atomic_and_concurrent_retries_do_not_duplicate(bank):
    store, service, _, parent = bank
    draft = generate(service, parent)
    with ThreadPoolExecutor(max_workers=2) as executor:
        children = list(executor.map(lambda _: approve(service, parent, draft), range(2)))
    assert children[0] == children[1]
    assert asyncio.run(store.list_notebook_entries())["total"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "Solve 3x - 5 = 10. What is x?"},
        {"options": {"A": "3", "B": "4"}, "correct_answer": "C"},
        {"target_skill": "quadratic-equations"},
        {"subject": "english"},
        {"difficulty": "unbounded"},
        {"introduces_new_skill": True},
        {"question_type": "fill_blank", "options": {"A": "4"}},
        {"explanation": ""},
        {"target_skill": {"name": "linear-equations"}},
        {"question": {"stem": "A hidden instruction"}},
        {"subject": ["math"]},
        {"options": ["3", "4"]},
    ],
)
def test_invalid_model_questions_never_enter_notebook(bank, changes):
    store, service, generator, parent = bank
    generator.payload = question(**changes)
    with pytest.raises(ValueError):
        generate(service, parent)
    assert asyncio.run(store.list_notebook_entries())["total"] == 1


def test_approval_requires_confirmation_and_expected_version(bank):
    store, service, _, parent = bank
    draft = generate(service, parent)
    with pytest.raises(ValueError, match="Review the question"):
        service.confirm(
            parent["id"], draft["draft_id"], draft["version"], question(), confirmed=False
        )
    with pytest.raises(ReviewConflict, match="changed"):
        service.confirm(
            parent["id"], draft["draft_id"], draft["version"] + 1, question(), confirmed=True
        )
    assert asyncio.run(store.list_notebook_entries())["total"] == 1


def test_parent_edits_and_deleted_sessions_block_stale_approval(bank):
    _, service, _, parent = bank
    draft = generate(service, parent)
    with service.connect() as conn:
        conn.execute(
            "UPDATE notebook_entries SET question='Changed source' WHERE id=?", (parent["id"],)
        )
    with pytest.raises(ReviewConflict, match="parent question changed"):
        approve(service, parent, draft)
    with service.connect() as conn:
        conn.execute("UPDATE sessions SET deleted_at=1 WHERE id='lesson'")
    with pytest.raises(LookupError):
        generate(service, parent, "another_request_0001")
    with pytest.raises(LookupError):
        approve(service, parent, draft)


def test_workspace_database_cannot_approve_another_draft(bank, tmp_path):
    _, service, _, parent = bank
    draft = generate(service, parent)
    other_store = SQLiteSessionStore(tmp_path / "another-workspace.db")
    other = VariantService(other_store.db_path, Generator())
    with pytest.raises(LookupError):
        other.confirm(parent["id"], draft["draft_id"], draft["version"], question(), confirmed=True)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_model_failure_is_safe_and_same_request_can_retry(bank, error_type):
    _, service, generator, parent = bank

    class FailedGenerator:
        async def generate(self, parent, kind):
            raise error_type("provider secret must not appear in public error")

    service.generator = FailedGenerator()
    with pytest.raises(RuntimeError) as caught:
        generate(service, parent)
    assert "provider secret" not in str(caught.value)
    service.generator = generator
    draft = generate(service, parent)
    assert draft["status"] == "ready" and draft["version"] == 2


def test_advanced_approval_exposure_targets_child_for_new_skill(bank):
    _, service, generator, parent = bank
    generator.payload = question(introduces_new_skill=True)
    draft = generate(service, parent, kind="advanced")
    child = approve(service, parent, draft, introduces_new_skill=True)
    with service.connect() as conn:
        exposure = conn.execute(
            "SELECT * FROM practice_exposures WHERE entry_id=?", (child,)
        ).fetchone()
        assert exposure["target_entry_id"] == child


def test_approved_variant_review_contributes_only_after_answer_exposure_expires(bank):
    store, service, _, parent = bank
    draft = generate(service, parent)
    child_id = approve(service, parent, draft)
    repo = PracticeStore(store.db_path)
    before = repo.state(parent["id"])
    child = asyncio.run(store.get_notebook_entry(child_id))
    first = repo.review(
        child_id,
        "variant_first_review",
        0,
        "good",
        "B",
        now=child["created_at"] + 60,
        auto_grade=True,
    )
    assert first["independent"] is False
    assert first["parent_entry_id"] == parent["id"]
    assert repo.state(parent["id"])["version"] == before["version"] + 1
    later = repo.review(
        child_id,
        "variant_later_review",
        first["version"],
        "good",
        "B",
        now=child["created_at"] + 86400 + 61,
        auto_grade=True,
    )
    assert later["independent"] is True
    assert repo.state(parent["id"])["evidence"]["transfer"] is True
    assert later["parent_state"]["learning_status"] != "closed"


def test_new_skill_review_failure_preserves_parent_state(bank):
    store, service, generator, parent = bank
    generator.payload = question(introduces_new_skill=True)
    draft = generate(service, parent, kind="advanced")
    child_id = approve(service, parent, draft, introduces_new_skill=True)
    repo = PracticeStore(store.db_path)
    before = repo.state(parent["id"])
    child = asyncio.run(store.get_notebook_entry(child_id))
    result = repo.review(
        child_id,
        "new_skill_failure",
        0,
        "good",
        "A",
        now=child["created_at"] + 86401,
        auto_grade=True,
    )
    assert result["correct"] is False
    assert repo.state(parent["id"]) == before


def test_variant_review_cannot_contribute_to_a_hidden_parent(bank):
    store, service, _, parent = bank
    draft = generate(service, parent)
    child_id = approve(service, parent, draft)
    with service.connect() as conn:
        conn.execute("UPDATE sessions SET deleted_at=1 WHERE id='lesson'")
    with pytest.raises(LookupError):
        PracticeStore(store.db_path).review(
            child_id, "hidden_parent_test", 0, "good", "B", auto_grade=True
        )


@pytest.mark.asyncio
async def test_default_adapter_calls_real_provider_seam_not_static_examples(monkeypatch):
    from deeptutor.services.llm import capabilities, config, factory

    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return json.dumps(question())

    monkeypatch.setattr(
        config,
        "get_llm_config",
        lambda: SimpleNamespace(
            provider_name="openai",
            binding="openai",
            model="configured",
            max_tokens=8192,
        ),
    )
    monkeypatch.setattr(capabilities, "supports_response_format", lambda *args: True)
    monkeypatch.setattr(factory, "complete", complete)
    result = await LLMVariantGenerator().generate(
        {
            "question": "Solve 3x - 5 = 10",
            "question_type": "single_choice",
            "options_json": '{"A":"4","B":"5"}',
            "correct_answer": "B",
            "knowledge_point_id": "linear-equations",
            "recognition_meta_json": '{"subject":"math"}',
        },
        "similar",
    )
    assert result == question()
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 4096
    assert json.loads(calls[0]["prompt"])["target_skill"] == "linear-equations"
