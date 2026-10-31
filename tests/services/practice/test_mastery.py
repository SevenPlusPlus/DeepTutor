"""Close decisions require delayed independent evidence, never UI self-certification."""

import asyncio
import json

import pytest

from deeptutor.services.practice.mastery import POLICY_VERSION
from deeptutor.services.practice.scheduler import DAY
from deeptutor.services.practice.storage import PracticeStore, ReviewConflict
from deeptutor.services.session.sqlite_store import SQLiteSessionStore

NOW = 2_000_000_000.0


@pytest.fixture
def bank(tmp_path):
    store = SQLiteSessionStore(tmp_path / "mastery.db")
    return store, PracticeStore(store.db_path)


def seed(store, key="parent", *, result="incorrect", question=None):
    async def create():
        await store.ensure_notebook_session("lesson", "Lesson")
        await store.upsert_notebook_entries(
            "lesson",
            [
                {
                    "question_id": key,
                    "question": question or f"Question {key}",
                    "question_type": "single_choice",
                    "options": {"A": "3", "B": "4"},
                    "correct_answer": "B",
                    "user_answer": "A",
                    "is_correct": False,
                    "result": result,
                }
            ],
        )
        return (await store.find_notebook_entry("lesson", key))["id"]

    return asyncio.run(create())


def variant(store, repo, parent, *, key="variant", new_skill=False):
    child = seed(store, key, result="ungraded")
    with repo.connect() as conn:
        conn.execute(
            "INSERT INTO practice_variants VALUES(?,?,?,?,?,?,?)",
            (
                child,
                parent,
                "advanced" if new_skill else "similar",
                "same-skill",
                int(new_skill),
                "approved",
                NOW,
            ),
        )
    return child


def answer(repo, entry, when, key, value="B", **kwargs):
    return repo.review(
        entry, key, repo.state(entry)["version"], "good", value, now=when, auto_grade=True, **kwargs
    )


def close_mistake(store, repo):
    parent = seed(store)
    child = variant(store, repo, parent)
    answer(repo, parent, NOW, "original-first")
    answer(repo, child, NOW + 2 * DAY, "variant-first")
    closed = answer(repo, parent, NOW + 10 * DAY, "delayed-final")
    assert closed["closed"]
    return parent, child


def test_delayed_original_plus_distinct_variant_closes_and_audits(bank):
    store, repo = bank
    parent, child = close_mistake(store, repo)
    state = repo.state(parent)
    assert state["learning_status"] == "closed"
    assert state["evidence"]["ready"]
    assert state["evidence"]["independent_successes"] == 2
    assert state["closed_at"] == NOW + 10 * DAY
    details = repo.mastery_details(parent)
    assert len(details["history"]) == 3
    assert details["history"][1]["entry_id"] == child
    assert details["decisions"][0]["policy_version"] == POLICY_VERSION
    assert details["decisions"][0]["action"] == "closed"
    assert parent not in repo.queue("UTC", now=NOW + 100 * DAY)


def test_recent_variant_resets_the_delay_not_just_original_dates(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent)
    answer(repo, parent, NOW, "original")
    answer(repo, child, NOW + 9 * DAY, "recent-variant")
    result = answer(repo, parent, NOW + 10 * DAY, "too-soon-final")
    assert result["evidence"]["spaced"] and result["evidence"]["transfer"]
    assert not result["closed"] and not result["evidence"]["delayed"]
    assert answer(repo, parent, NOW + 17 * DAY, "proper-final")["closed"]


def test_answer_exposure_survives_abandoned_submission_and_new_tab(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent)
    answer(repo, parent, NOW, "first-original")
    answer(repo, child, NOW + 2 * DAY, "first-variant")
    first = repo.expose(parent, "help-without-submitting", "answer", now=NOW + 10 * DAY)
    assert repo.expose(parent, "help-without-submitting", "answer", now=NOW + 11 * DAY) == first
    with pytest.raises(ReviewConflict):
        repo.expose(parent, "help-without-submitting", "hint", now=NOW + 10 * DAY)
    result = answer(repo, parent, NOW + 10 * DAY + 60, "fresh-tab")
    assert not result["independent"] and not result["closed"]
    second = answer(repo, parent, NOW + 10 * DAY + 120, "another-tab")
    assert not second["independent"] and not second["closed"]


def test_feedback_does_not_make_same_day_repeat_independent(bank):
    store, repo = bank
    parent = seed(store)
    first = answer(repo, parent, NOW, "first")
    again = answer(repo, parent, NOW + 600, "repeat")
    assert first["independent"] and not again["independent"]
    assert again["evidence"]["independent_successes"] == 1


def test_assisted_and_subjective_results_never_manufacture_mastery(bank):
    store, repo = bank
    parent = seed(store)
    result = answer(repo, parent, NOW, "hinted", hint_used=True)
    assert not result["independent"]
    result = answer(repo, parent, NOW + 2 * DAY, "revealed", answer_revealed=True)
    assert not result["independent"]
    result = answer(repo, parent, NOW + 10 * DAY, "self-grade", self_report=True)
    assert result["correct"] is None and not result["independent"]
    assert not repo.state(parent)["evidence"]["ready"]


def test_correct_early_spotcheck_preserves_close_snapshot_and_original_date(bank):
    store, repo = bank
    parent, _ = close_mistake(store, repo)
    before = repo.state(parent)
    result = answer(repo, parent, NOW + 11 * DAY, "early-spotcheck", attempt_kind="spotcheck")
    assert result["learning_status"] == "closed" and not result["closed"]
    assert result["closed_at"] == before["closed_at"]
    assert result["evidence"] == before["evidence"]
    assert len(repo.mastery_details(parent)["decisions"]) == 1


def test_confirmed_failure_reopens_and_old_successes_do_not_reclose(bank):
    store, repo = bank
    parent, _ = close_mistake(store, repo)
    result = answer(
        repo, parent, NOW + 12 * DAY, "failed-spotcheck", value="A", attempt_kind="spotcheck"
    )
    assert result["reopened"] and result["learning_status"] == "open"
    assert result["closed_at"] is None
    again = answer(repo, parent, NOW + 13 * DAY, "restart")
    assert not again["closed"] and again["evidence"]["independent_successes"] == 1
    assert [d["action"] for d in repo.mastery_details(parent)["decisions"]] == [
        "reopened",
        "closed",
    ]


def test_new_skill_failure_does_not_change_parent_close_or_version(bank):
    store, repo = bank
    parent, _ = close_mistake(store, repo)
    child = variant(store, repo, parent, key="new-skill", new_skill=True)
    before = repo.state(parent)
    result = answer(repo, child, NOW + 12 * DAY, "advanced-failure", value="A")
    assert not result["reopened"] and result["parent_state"] == before
    assert repo.state(parent) == before
    assert repo.state(child)["is_mistake"] == 1


def test_variant_submission_invalidates_open_parent_tab(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent)
    old = repo.state(parent)["version"]
    answer(repo, child, NOW, "variant-review")
    with pytest.raises(ReviewConflict):
        repo.review(parent, "stale-parent", old, "good", "B", now=NOW + 2 * DAY)


def test_auto_grading_failure_retry_uses_original_requested_rating(bank):
    store, repo = bank
    parent = seed(store)
    request = (parent, "auto-failure", 0, "good", "A")
    first = repo.review(*request, now=NOW, auto_grade=True)
    assert first["rating"] == "again" and first["requested_rating"] == "good"
    assert repo.review(*request, now=NOW + 1, auto_grade=True) == first
    assert repo.state(parent)["review_count"] == 1
    with pytest.raises(ReviewConflict):
        repo.review(parent, "auto-failure", 0, "again", "A", now=NOW, auto_grade=True)
    with pytest.raises(ValueError):
        repo.review(parent, "legacy-invalid", 1, "good", "A", now=NOW)


def test_queue_and_summary_exclude_nonmistakes_closed_paused_and_resolved(bank):
    store, repo = bank
    active = seed(store, "active")
    paused = seed(store, "paused")
    resolved = seed(store, "resolved")
    bank_only = seed(store, "bank-only", result="ungraded")
    closed, _ = close_mistake(store, repo)
    repo.update_learning(paused, repo.state(paused)["version"], paused=True)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (resolved,))
        conn.execute("UPDATE practice_review_state SET due_at=100")
    assert repo.queue("UTC", now=NOW + 100 * DAY) == [active]
    summary = repo.overview("UTC", now=NOW + 100 * DAY)
    assert summary["due"] == 1 and summary["closed"] == 1 and summary["paused"] == 2
    assert bank_only not in repo.queue("UTC", now=NOW + 100 * DAY)
    assert closed not in repo.queue("UTC", now=NOW + 100 * DAY)
    general = repo.queue("UTC", now=NOW + 100 * DAY, review_scope="all")
    assert bank_only in general
    assert not {closed, paused, resolved}.intersection(general)
    assert repo.overview("UTC", now=NOW + 100 * DAY, review_scope="all")["due"] == len(general)
    repo.update_learning(paused, repo.state(paused)["version"], paused=False)
    assert paused in repo.queue("UTC", now=NOW + 100 * DAY)
    assert repo.queue("UTC", session_ids=[], now=NOW + 100 * DAY) == []


def test_general_review_keeps_srs_retries_without_bypassing_mastery_evidence(bank):
    store, repo = bank
    general = seed(store, "general-review")
    daily = seed(store, "daily-review")
    failed = repo.review(general, "general-failed", 0, "again", "A", now=NOW, review_scope="all")
    assert 0 < failed["due_at"] - NOW < 15 * 60
    default_failure = repo.review(daily, "daily-failed", 0, "again", "A", now=NOW)
    assert default_failure["due_at"] == NOW + DAY
    with pytest.raises(ReviewConflict):
        repo.review(general, "general-failed", 0, "again", "A", now=NOW)
    with pytest.raises(ValueError):
        repo.review(general, "general-invalid", 1, "good", "A", now=NOW, review_scope="all")
    # Feedback shown after the first attempt remains exposure in the all scope.
    corrected = repo.review(
        general, "general-corrected", 1, "good", "B", now=NOW + 900, review_scope="all"
    )
    assert corrected["correct"] is True
    assert not corrected["independent"] and not corrected["closed"]
    assert corrected["due_at"] == NOW + 900 + 3 * DAY
    assert corrected["evidence"]["independent_successes"] == 0
    assert repo.mastery_details(general)["decisions"] == []


def test_recommendation_budget_filters_and_explicit_early_recall(bank):
    store, repo = bank
    first = seed(store, "fractions", question="Find the fraction")
    second = seed(store, "geometry", question="Find the angle")
    with repo.connect() as conn:
        conn.execute("UPDATE practice_review_state SET due_at=?", (NOW - DAY,))
        conn.execute(
            "UPDATE notebook_entries SET recognition_meta_json=?",
            (json.dumps({"subject": "math"}),),
        )
    results = repo.recommendations("UTC", budget_minutes=2, subject="math", now=NOW)
    assert sum(item["estimated_minutes"] for item in results) <= 2
    assert all(item["reasons"] for item in results)
    assert [
        item["entry_id"] for item in repo.recommendations("UTC", query="fraction", now=NOW)
    ] == [first]
    assert repo.recommendations("UTC", subject="english", now=NOW) == []
    with repo.connect() as conn:
        conn.execute(
            "UPDATE practice_review_state SET due_at=? WHERE entry_id=?", (NOW + DAY, second)
        )
    assert repo.recommendations("UTC", query="angle", now=NOW) == []
    assert (
        repo.recommendations("UTC", query="angle", include_early=True, now=NOW)[0]["entry_id"]
        == second
    )


def test_hidden_parent_cannot_receive_evidence_through_standalone_variant(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET session_id=NULL WHERE id=?", (child,))
        conn.execute("UPDATE sessions SET deleted_at=1 WHERE id='lesson'")
    assert not repo.is_visible(child)
    with pytest.raises(LookupError):
        answer(repo, child, NOW, "hidden-parent")


def test_legacy_history_and_manual_resolution_are_not_fabricated_close_evidence(bank):
    store, repo = bank
    parent = seed(store)
    with repo.connect() as conn:
        conn.execute(
            "INSERT INTO practice_review_events VALUES(?,?,?,?,?,?)",
            ("legacy", parent, "good", "B", NOW, '{"mastered":true}'),
        )
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (parent,))
    assert repo.state(parent)["learning_status"] == "open"
    assert repo.state(parent)["evidence"]["independent_successes"] == 0
    assert repo.mastery_details(parent)["decisions"] == []


def test_resume_restores_legacy_manual_resolved_mistake(bank):
    store, repo = bank
    parent = seed(store)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (parent,))
        conn.execute(
            "UPDATE practice_review_state SET due_at=? WHERE entry_id=?", (NOW - DAY, parent)
        )
    assert repo.queue("UTC", now=NOW) == []
    repo.update_learning(parent, repo.state(parent)["version"], paused=False)
    assert repo.queue("UTC", now=NOW) == [parent]
    assert repo.state(parent)["learning_status"] != "closed"


def test_standalone_variant_inherits_course_scope_but_not_hidden_parent(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent, new_skill=True)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET session_id=NULL WHERE id=?", (child,))
    answer(repo, child, NOW, "new-skill-wrong", value="A")
    scoped = repo.queue("UTC", session_ids=["lesson"], course_id="course-a", now=NOW + DAY)
    assert child in scoped
    assert repo.recommendations("UTC", session_ids=["lesson"], course_id="course-a", now=NOW + DAY)
    with repo.connect() as conn:
        conn.execute("UPDATE sessions SET deleted_at=1 WHERE id='lesson'")
    assert child not in repo.queue("UTC", now=NOW + DAY)


def test_question_edit_reopens_and_invalidates_approved_variants(bank):
    store, repo = bank
    parent, child = close_mistake(store, repo)
    with repo.connect() as conn:
        conn.execute(
            "UPDATE notebook_entries SET question='Revised question',updated_at=? WHERE id=?",
            (NOW + 11 * DAY, parent),
        )
        assert (
            conn.execute(
                "SELECT validation_status FROM practice_variants WHERE entry_id=?", (child,)
            ).fetchone()[0]
            == "stale"
        )
    assert repo.state(parent)["learning_status"] == "open"
    result = answer(repo, parent, NOW + 12 * DAY, "new-version-first")
    assert not result["closed"] and result["evidence"]["independent_successes"] == 1


def test_existing_review_schema_migrates_without_promoting_old_evidence(tmp_path):
    import sqlite3

    from deeptutor.services.practice.storage import initialize_practice

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE notebook_entries(id INTEGER PRIMARY KEY,is_correct INTEGER,result TEXT,
            created_at REAL,updated_at REAL,resolved INTEGER,question TEXT,options_json TEXT,correct_answer TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE practice_review_state(entry_id INTEGER PRIMARY KEY,is_mistake INTEGER NOT NULL DEFAULT 1,
            first_wrong_at REAL NOT NULL,due_at REAL NOT NULL,interval_days REAL NOT NULL DEFAULT 1,
            ease REAL NOT NULL DEFAULT 2.5,streak INTEGER NOT NULL DEFAULT 0,lapses INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,last_review_at REAL,version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE practice_review_events(request_id TEXT PRIMARY KEY,entry_id INTEGER,rating TEXT,
            answer TEXT,reviewed_at REAL,outcome_json TEXT);
        INSERT INTO notebook_entries VALUES(1,0,'incorrect',10,20,1,'Legacy','{}','',1);
        INSERT INTO practice_review_state(entry_id,first_wrong_at,due_at,review_count,version) VALUES(1,10,50,3,4);
        INSERT INTO practice_review_events VALUES('old',1,'good','A',30,'{"mastered":true}');
    """
    )
    initialize_practice(conn)
    initialize_practice(conn)
    conn.commit()
    state = PracticeStore(path).state(1)
    assert state["due_at"] == 50 and state["review_count"] == 3 and state["version"] == 4
    assert state["learning_status"] == "open" and not state["evidence"]["ready"]
    assert (
        conn.execute(
            "SELECT outcome_json FROM practice_review_events WHERE request_id='old'"
        ).fetchone()[0]
        == '{"mastered":true}'
    )
    conn.close()


def test_runtime_schema_upgrade_preserves_recognition_and_mastery_history(bank):
    store, repo = bank
    parent, _ = close_mistake(store, repo)
    paused = seed(store, "paused-during-upgrade")
    repo.update_learning(paused, repo.state(paused)["version"], paused=True)
    repo.expose(parent, "upgrade-answer-exposure", "answer", now=NOW + 11 * DAY)
    with repo.connect() as conn:
        conn.execute(
            "UPDATE notebook_entries SET course_id=?,question_images_json=?,"
            "source_locator_json=?,answer_origin=?,recognition_meta_json=? WHERE id=?",
            (
                "legacy-course",
                json.dumps([{"id": "original-photo", "url": "/attachments/original.png"}]),
                json.dumps({"page_numbers": [2]}),
                "document",
                json.dumps({"subject": "math", "grade": "5"}),
                parent,
            ),
        )
        # These tables were introduced by the newer session/reading runtime.
        conn.execute("DROP TABLE turn_submissions")
        conn.execute("DROP TABLE reading_quiz_rewards")
    before_entry = asyncio.run(store.get_notebook_entry(parent))
    before_mastery = repo.mastery_details(parent)
    before_paused = repo.state(paused)

    upgraded = SQLiteSessionStore(store.db_path)
    restored = PracticeStore(upgraded.db_path)
    assert asyncio.run(upgraded.get_notebook_entry(parent)) == before_entry
    assert restored.mastery_details(parent) == before_mastery
    assert restored.state(paused) == before_paused
    assert restored.state(parent)["learning_status"] == "closed"
    session_id = asyncio.run(upgraded.reserve_submission("upgrade-submission", "digest"))
    assert asyncio.run(upgraded.get_session(session_id)) is not None
    with restored.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_quiz_rewards'"
        ).fetchone()
        assert (
            conn.execute(
                "SELECT kind FROM practice_exposures WHERE request_id='upgrade-answer-exposure'"
            ).fetchone()[0]
            == "answer"
        )


def test_new_skill_mistake_can_complete_its_own_mastery_loop(bank):
    store, repo = bank
    base, _ = close_mistake(store, repo)
    base_before = repo.state(base)
    extension = variant(store, repo, base, key="extension-target", new_skill=True)
    answer(repo, extension, NOW + 12 * DAY, "extension-failure", value="A")
    answer(repo, extension, NOW + 13 * DAY, "extension-original-first")
    transfer = variant(store, repo, extension, key="extension-transfer")
    answer(repo, transfer, NOW + 15 * DAY, "extension-transfer-pass")
    final = answer(repo, extension, NOW + 23 * DAY, "extension-delayed-final")
    assert final["closed"] and final["learning_status"] == "closed"
    assert repo.state(base) == base_before


def test_grade_filter_is_hard_and_legacy_resolved_is_paused(bank):
    store, repo = bank
    fifth = seed(store, "grade-five")
    seventh = seed(store, "grade-seven")
    with repo.connect() as conn:
        conn.execute("UPDATE practice_review_state SET due_at=?", (NOW - DAY,))
        for entry_id, grade in [(fifth, "5"), (seventh, "7")]:
            conn.execute(
                "UPDATE notebook_entries SET recognition_meta_json=? WHERE id=?",
                (json.dumps({"grade": grade, "subject": "math"}), entry_id),
            )
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (fifth,))
    assert repo.state(fifth)["paused"]
    assert repo.overview("UTC", now=NOW)["paused"] == 1
    assert repo.recommendations("UTC", grade="5", now=NOW) == []
    repo.update_learning(fifth, repo.state(fifth)["version"], paused=False)
    assert [item["entry_id"] for item in repo.recommendations("UTC", grade="5", now=NOW)] == [fifth]
    assert repo.recommendations("UTC", grade="6", now=NOW) == []


def test_hidden_parent_also_blocks_exposure_and_pause(bank):
    store, repo = bank
    parent = seed(store)
    child = variant(store, repo, parent, new_skill=True)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET session_id=NULL WHERE id=?", (child,))
    answer(repo, child, NOW, "child-mistake", value="A")
    with repo.connect() as conn:
        conn.execute("UPDATE sessions SET deleted_at=1 WHERE id='lesson'")
    assert not repo.is_visible(child)
    with pytest.raises(LookupError):
        repo.expose(child, "hidden-exposure", "answer", now=NOW)
    with pytest.raises(LookupError):
        repo.update_learning(child, repo.state(child)["version"], paused=True)


def test_assisted_correctness_does_not_inflate_recall_intervals(bank):
    store, repo = bank
    parent = seed(store)
    result = answer(repo, parent, NOW, "assisted-first", hint_used=True)
    assert not result["independent"]
    assert result["streak"] == 0 and result["interval_days"] < 3


def test_new_external_failure_reopens_even_when_original_result_was_already_wrong(bank):
    store, repo = bank
    parent, _ = close_mistake(store, repo)
    before = repo.state(parent)
    with repo.connect() as conn:
        conn.execute("UPDATE notebook_entries SET resolved=1 WHERE id=?", (parent,))
        conn.execute(
            "UPDATE notebook_entries SET is_correct=0,result='incorrect',attempt_count=attempt_count+1,updated_at=? WHERE id=?",
            (NOW + 11 * DAY, parent),
        )
    state = repo.state(parent)
    assert state["learning_status"] == "open" and not state["paused"]
    assert state["version"] > before["version"]
    assert state["due_at"] == NOW + 12 * DAY
    assert repo.mastery_details(parent)["decisions"][0]["reason"] == "source_failure"


def test_returned_scores_preserve_diversity_when_api_merges_ranked_lists(bank):
    store, repo = bank
    first = seed(store, "fraction-one")
    same = seed(store, "fraction-two")
    different = seed(store, "geometry-one")
    with repo.connect() as conn:
        conn.execute("UPDATE practice_review_state SET due_at=?", (NOW - DAY,))
        for entry_id, topic in [(first, "fraction"), (same, "fraction"), (different, "geometry")]:
            conn.execute(
                "UPDATE notebook_entries SET knowledge_point_id=? WHERE id=?", (topic, entry_id)
            )
    results = repo.recommendations("UTC", budget_minutes=6, now=NOW)
    assert [item["entry_id"] for item in results] == [first, different, same]
    assert [item["entry_id"] for item in sorted(results, key=lambda row: -row["score"])] == [
        first,
        different,
        same,
    ]
