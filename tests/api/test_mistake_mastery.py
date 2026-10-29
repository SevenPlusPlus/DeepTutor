"""Exercise the real mistake workflow through its HTTP and scoped-store seams."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import practice, question_notebook
from deeptutor.services.practice.scheduler import DAY
from deeptutor.services.practice.storage import PracticeStore
from deeptutor.services.practice.variants import VariantService
from deeptutor.services.session.sqlite_store import SQLiteSessionStore


def variant_question():
    return {
        "question": "Solve 2x + 3 = 11. What is x?",
        "question_type": "single_choice",
        "options": {"A": "3", "B": "4"},
        "correct_answer": "B",
        "explanation": "2x = 8, so x = 4.",
        "difficulty": "medium",
        "knowledge_point": "linear-equations",
        "target_skill": "linear-equations",
        "introduces_new_skill": False,
        "subject": "math",
    }


class Generator:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def generate(self, parent, kind):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private provider diagnostic must stay private")
        return variant_question()


@pytest.fixture
def bank(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "mastery-api.db")
    repo = PracticeStore(store.db_path)
    generator = Generator()
    variants = VariantService(store.db_path, generator)

    async def sessions(_store, course_id):
        return None if not course_id else ["lesson"] if course_id == "algebra" else []

    for module in (practice, question_notebook):
        monkeypatch.setattr(module, "get_sqlite_session_store", lambda: store)
        monkeypatch.setattr(module, "_course_session_ids", sessions)
    monkeypatch.setattr(practice, "_variant_service", lambda: variants)
    monkeypatch.setattr(practice, "current_workspace_id", lambda: "workspace-one")
    app = FastAPI()
    app.include_router(question_notebook.router, prefix="/bank")
    app.include_router(practice.router, prefix="/practice")
    with TestClient(app) as client:
        yield SimpleNamespace(store=store, repo=repo, client=client, generator=generator)


def seed(
    bank, key="original", *, session="lesson", enrolled=True, subject="math", grade="7", **changes
):
    async def run():
        await bank.store.ensure_notebook_session(session, "Learning")
        await bank.store.upsert_notebook_entries(
            session,
            [
                {
                    "question_id": key,
                    "question": "Solve 3x - 5 = 10. " + key,
                    "question_type": "single_choice",
                    "options": {"A": "4", "B": "5"},
                    "correct_answer": "B",
                    "user_answer": "A",
                    "is_correct": False,
                    "result": "incorrect" if enrolled else "ungraded",
                    "knowledge_point_id": "linear-equations",
                    **changes,
                }
            ],
        )
        return (await bank.store.find_notebook_entry(session, key))["id"]

    entry_id = asyncio.run(run())
    with bank.repo.connect() as conn:
        conn.execute(
            "UPDATE notebook_entries SET recognition_meta_json=?,created_at=? WHERE id=?",
            (json.dumps({"subject": subject, "grade": grade}), time.time() - 40 * DAY, entry_id),
        )
        conn.execute("UPDATE practice_review_state SET due_at=0 WHERE entry_id=?", (entry_id,))
    return entry_id


def review_payload(bank, entry_id, key="review_request_0001", **changes):
    return {
        "request_id": key,
        "version": bank.repo.state(entry_id)["version"],
        "rating": "good",
        "answer": "B",
        "auto_grade": True,
        "timezone": "Asia/Shanghai",
        **changes,
    }


def generate(bank, entry_id, **changes):
    return bank.client.post(
        f"/practice/questions/{entry_id}/variants/generate",
        json={
            "request_id": "generate_request_0001",
            "kind": "similar",
            **changes,
        },
    )


def confirm_payload(draft, **changes):
    return {
        "draft_id": draft["draft_id"],
        "expected_version": draft["version"],
        "confirmed": True,
        "question": variant_question(),
        **changes,
    }


def closed_mistake(bank):
    parent = seed(bank, "closed-parent")
    child = seed(bank, "approved-child", enrolled=False)
    now = time.time()
    with bank.repo.connect() as conn:
        conn.execute(
            "INSERT INTO practice_variants VALUES(?,?,?,?,?,?,?)",
            (
                child,
                parent,
                "similar",
                "linear-equations",
                0,
                "approved",
                now - 35 * DAY,
            ),
        )
    for entry_id, when, request_id in (
        (parent, now - 30 * DAY, "first_success"),
        (child, now - 28 * DAY, "transfer_success"),
        (parent, now - 20 * DAY, "delayed_success"),
    ):
        bank.repo.review(
            entry_id,
            request_id,
            bank.repo.state(entry_id)["version"],
            "good",
            "B",
            now=when,
            auto_grade=True,
        )
    assert bank.repo.state(parent)["learning_status"] == "closed"
    return parent, child


def test_recommendations_honor_budget_and_active_mistake_scope(bank):
    active = [seed(bank, f"active-{n}") for n in range(4)]
    paused = seed(bank, "paused")
    bank.repo.update_learning(paused, 0, True)
    legacy = seed(bank, "legacy")
    with bank.repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (legacy,))
    seed(bank, "not-a-mistake", enrolled=False)
    closed, _ = closed_mistake(bank)
    response = bank.client.get("/practice/recommendations?budget_minutes=5&timezone=Asia/Shanghai")
    assert response.status_code == 200
    result = response.json()
    ids = [item["entry"]["id"] for item in result["items"]]
    assert ids and set(ids) <= set(active)
    assert result["estimated_minutes"] == sum(
        item["recommendation"]["estimated_minutes"] for item in result["items"]
    )
    assert result["estimated_minutes"] <= 5
    assert all(item["recommendation"]["reasons"] for item in result["items"])
    queue_ids = {item["entry"]["id"] for item in bank.client.get("/practice/queue").json()}
    assert queue_ids == set(active)
    assert not {paused, legacy, closed} & queue_ids


def test_recommendations_intersect_subject_query_and_category(bank):
    selected = seed(bank, "fractions-urgent", question="Find a common denominator")
    other_subject = seed(
        bank, "fractions-other", subject="english", question="Find a common denominator"
    )
    other_grade = seed(bank, "fractions-fifth", grade="5", question="Find a common denominator")
    seed(bank, "unrelated", question="A different concept")
    category = bank.client.post("/bank/categories", json={"name": "漏看条件"})
    assert category.status_code == 201
    category_id = category.json()["id"]
    for entry_id in (selected, other_subject, other_grade):
        assert (
            bank.client.post(
                f"/bank/entries/{entry_id}/categories", json={"category_id": category_id}
            ).status_code
            == 200
        )
    result = bank.client.get(
        "/practice/recommendations",
        params={
            "subject": "math",
            "grade": "7",
            "query": "denominator 漏看条件",
            "category_id": category_id,
        },
    )
    assert result.status_code == 200
    assert [row["entry"]["id"] for row in result.json()["items"]] == [selected]
    assert bank.client.get("/practice/recommendations?subject=physics").json()["items"] == []


def test_recommendation_group_preserves_knowledge_point_diversity(bank):
    seed(bank, "same-skill-one", knowledge_point_id="same-skill")
    urgent = seed(bank, "same-skill-urgent", knowledge_point_id="same-skill")
    different = seed(bank, "different-skill", knowledge_point_id="different-skill")
    with bank.repo.connect() as conn:
        conn.execute("UPDATE practice_review_state SET lapses=1 WHERE entry_id=?", (urgent,))
    response = bank.client.get("/practice/recommendations?budget_minutes=5&limit=2")
    assert response.status_code == 200
    assert [item["entry"]["id"] for item in response.json()["items"]] == [urgent, different]


def test_explicit_early_practice_is_opt_in_and_explained(bank):
    entry = seed(bank)
    with bank.repo.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET due_at=? WHERE entry_id=?",
            (time.time() + 7 * DAY, entry),
        )
    assert bank.client.get("/practice/recommendations").json()["items"] == []
    early = bank.client.get("/practice/recommendations?include_early=true").json()["items"]
    assert early[0]["entry"]["id"] == entry
    assert "Early practice requested" in early[0]["recommendation"]["reasons"]


def test_summary_separates_open_verify_closed_and_paused(bank):
    seed(bank, "open")
    verified = seed(bank, "verify")
    with bank.repo.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET learning_status='verify' WHERE entry_id=?",
            (verified,),
        )
    paused = seed(bank, "paused")
    bank.repo.update_learning(paused, 0, True)
    closed_mistake(bank)
    seed(bank, "bank-only", enrolled=False)
    response = bank.client.get("/practice/summary?timezone=Asia/Shanghai")
    assert response.status_code == 200
    result = response.json()
    assert {key: result[key] for key in ("open", "verify", "closed", "paused")} == {
        "open": 1,
        "verify": 1,
        "closed": 1,
        "paused": 1,
    }
    assert result["mistakes"] == 4 and result["due"] == 2


def test_pause_requires_current_version_and_resume_restores_legacy_resolved(bank):
    entry = seed(bank)
    with bank.repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (entry,))
    paused = bank.client.post(
        f"/practice/questions/{entry}/learning", json={"version": 0, "paused": True}
    )
    assert paused.status_code == 200 and paused.json()["paused"] is True
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/learning", json={"version": 0, "paused": False}
        ).status_code
        == 409
    )
    resumed = bank.client.post(
        f"/practice/questions/{entry}/learning",
        json={"version": paused.json()["version"], "paused": False},
    )
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    assert bank.client.get(f"/practice/questions/{entry}").json()["entry"]["resolved"] is False
    assert [row["entry"]["id"] for row in bank.client.get("/practice/queue").json()] == [entry]


def test_exposure_idempotency_and_conflict_survive_reopened_attempt(bank):
    entry = seed(bank)
    payload = {"request_id": "exposure_request_0001", "kind": "answer"}
    first = bank.client.post(f"/practice/questions/{entry}/exposure", json=payload)
    assert first.status_code == 200
    assert (
        bank.client.post(f"/practice/questions/{entry}/exposure", json=payload).json()
        == first.json()
    )
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/exposure", json={**payload, "kind": "hint"}
        ).status_code
        == 409
    )
    result = bank.client.post(
        f"/practice/questions/{entry}/review", json=review_payload(bank, entry)
    )
    assert result.status_code == 200 and result.json()["independent"] is False
    assert result.json()["closed"] is False


def test_auto_grade_forces_incorrect_rating_and_submission_is_retry_safe(bank):
    entry = seed(bank)
    payload = review_payload(bank, entry, answer="A")
    first = bank.client.post(f"/practice/questions/{entry}/review", json=payload)
    assert first.status_code == 200
    assert first.json()["correct"] is False and first.json()["rating"] == "again"
    assert (
        bank.client.post(f"/practice/questions/{entry}/review", json=payload).json() == first.json()
    )
    assert bank.repo.state(entry)["review_count"] == 1
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/review", json={**payload, "answer": "B"}
        ).status_code
        == 409
    )
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/review",
            json={**payload, "request_id": "stale_request_0001"},
        ).status_code
        == 409
    )


def test_numeric_fill_blank_is_objectively_graded_without_executing_input(bank):
    entry = seed(
        bank,
        question="One half as a decimal?",
        question_type="fill_blank",
        options={},
        correct_answer="1/2",
    )
    assert bank.client.post(
        f"/practice/questions/{entry}/check", json={"answer": "0.5"}
    ).json() == {"correct": True}
    assert bank.client.post(
        f"/practice/questions/{entry}/check", json={"answer": "1+1"}
    ).json() == {"correct": False}
    result = bank.client.post(
        f"/practice/questions/{entry}/review", json=review_payload(bank, entry, answer="0.5")
    )
    assert result.status_code == 200 and result.json()["independent"] is True
    assert result.json()["learning_status"] == "verify" and result.json()["closed"] is False


def test_mastery_endpoint_preserves_delayed_transfer_history_and_reopen_decision(bank):
    parent, child = closed_mistake(bank)
    response = bank.client.get(f"/practice/questions/{parent}/mastery")
    assert response.status_code == 200
    details = response.json()
    assert details["state"]["learning_status"] == "closed" and details["evidence"]["ready"]
    assert len(details["history"]) == 3 and child in {
        attempt["entry_id"] for attempt in details["history"]
    }
    assert details["decisions"][0]["action"] == "closed"
    result = bank.client.post(
        f"/practice/questions/{parent}/review",
        json=review_payload(bank, parent, answer="A", attempt_kind="spotcheck"),
    )
    assert result.status_code == 200 and result.json()["reopened"] is True
    details = bank.client.get(f"/practice/questions/{parent}/mastery").json()
    assert [decision["action"] for decision in details["decisions"]] == ["reopened", "closed"]


def test_variant_routes_generate_review_and_confirm_one_linked_question(bank):
    parent = seed(bank)
    first = generate(bank, parent)
    assert first.status_code == 200
    draft = first.json()
    assert draft["status"] == "ready" and draft["parent_entry_id"] == parent
    assert draft["target_skill"] == "linear-equations" and draft["correct_answer"] == "B"
    assert generate(bank, parent).json() == draft and bank.generator.calls == 1
    assert bank.client.get("/bank/entries").json()["total"] == 1
    payload = confirm_payload(draft)
    response = bank.client.post(f"/practice/questions/{parent}/variants/confirm", json=payload)
    assert response.status_code == 200
    child = response.json()
    assert child["entry"]["question"] == variant_question()["question"]
    assert child["entry"]["recognition_meta"]["variant"]["parent_entry_id"] == parent
    assert child["state"]["is_mistake"] == 0
    assert (
        bank.client.post(f"/practice/questions/{parent}/variants/confirm", json=payload).json()
        == child
    )
    assert bank.client.get("/bank/entries").json()["total"] == 2
    practiced = bank.client.post(
        f"/practice/questions/{child['entry']['id']}/review",
        json=review_payload(bank, child["entry"]["id"]),
    )
    assert practiced.status_code == 200 and practiced.json()["independent"] is False


def test_variant_route_validates_payload_versions_and_safe_model_errors(bank):
    parent = seed(bank)
    assert generate(bank, parent, kind="unknown").status_code == 422
    assert generate(bank, 999999).status_code == 404
    draft = generate(bank, parent).json()
    assert generate(bank, parent, kind="advanced").status_code == 409
    for patch, expected in (
        ({"confirmed": False}, 422),
        ({"expected_version": draft["version"] + 1}, 409),
        ({"draft_id": "missing"}, 404),
    ):
        response = bank.client.post(
            f"/practice/questions/{parent}/variants/confirm", json=confirm_payload(draft, **patch)
        )
        assert response.status_code == expected
    bank.generator.fail = True
    failed = generate(bank, parent, request_id="failed_generator_0001")
    assert failed.status_code == 503 and "private provider diagnostic" not in failed.text


def test_question_bank_filters_each_learning_state_and_legacy_archive(bank):
    opened = seed(bank, "opened")
    verify = seed(bank, "verify")
    paused = seed(bank, "paused")
    legacy = seed(bank, "legacy")
    closed, _ = closed_mistake(bank)
    with bank.repo.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET learning_status='verify' WHERE entry_id=?", (verify,)
        )
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (legacy,))
    bank.repo.update_learning(paused, 0, True)
    expected = {
        "active": {opened, verify},
        "open": {opened},
        "verify": {verify},
        "closed": {closed},
        "paused": {paused, legacy},
    }
    for state, ids in expected.items():
        response = bank.client.get("/bank/entries", params={"learning_status": state})
        assert response.status_code == 200
        assert {row["id"] for row in response.json()["items"]} == ids
        assert response.json()["total"] == len(ids)
    assert bank.client.get("/bank/entries?learning_status=invalid").status_code == 422


def test_question_bank_subject_grade_intersection_includes_fixed_variant_grade(bank):
    parent = seed(bank, "year-seven")
    seed(bank, "year-five", grade="5")
    seed(bank, "english-seven", subject="english")
    draft = generate(bank, parent).json()
    child = bank.client.post(
        f"/practice/questions/{parent}/variants/confirm",
        json=confirm_payload(draft, question={**variant_question(), "grade": "5"}),
    ).json()["entry"]
    assert child["recognition_meta"]["grade"] == "7"
    response = bank.client.get("/bank/entries?subject=math&grade=7")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["items"]} == {parent, child["id"]}
    active = bank.client.get("/bank/entries?subject=math&grade=7&learning_status=active").json()
    assert {row["id"] for row in active["items"]} == {parent}


def test_variant_inherits_parent_session_course_scope_and_hidden_parent_visibility(bank):
    parent = seed(bank)
    seed(bank, "other-course", session="other-lesson")
    draft = generate(bank, parent).json()
    child = bank.client.post(
        f"/practice/questions/{parent}/variants/confirm", json=confirm_payload(draft)
    ).json()["entry"]["id"]
    scoped = bank.client.get("/bank/entries?course_id=algebra")
    assert scoped.status_code == 200
    assert {row["id"] for row in scoped.json()["items"]} == {parent, child}
    with bank.repo.connect() as conn:
        conn.execute("UPDATE sessions SET deleted_at=? WHERE id='lesson'", (time.time(),))
    assert not {parent, child} & {
        row["id"] for row in bank.client.get("/bank/entries").json()["items"]
    }
    assert bank.client.get(f"/practice/questions/{child}").status_code == 404
    assert bank.client.get(f"/practice/questions/{child}/mastery").status_code == 404
    assert bank.client.get(f"/bank/entries/{child}").status_code == 404


def test_unknown_entries_and_invalid_review_inputs_return_bounded_http_errors(bank):
    for path in ("/practice/questions/999999", "/practice/questions/999999/mastery"):
        assert bank.client.get(path).status_code == 404
    assert (
        bank.client.post(
            "/practice/questions/999999/exposure",
            json={"request_id": "exposure_missing_0001", "kind": "answer"},
        ).status_code
        == 404
    )
    entry = seed(bank)
    assert bank.client.get("/practice/recommendations?budget_minutes=1").status_code == 422
    assert bank.client.get("/practice/recommendations?timezone=Unknown/Zone").status_code == 422
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/review",
            json=review_payload(bank, entry, request_id="short"),
        ).status_code
        == 422
    )
    assert (
        bank.client.post(
            f"/practice/questions/{entry}/review",
            json=review_payload(bank, entry, rating="invented"),
        ).status_code
        == 422
    )
