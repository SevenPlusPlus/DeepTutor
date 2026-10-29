"""Explainable cold-start ranking under a short daily time budget."""

from __future__ import annotations

import json

from .scheduler import DAY


def rank_recommendations(
    rows: list[dict],
    *,
    now: float,
    budget_minutes: int,
    limit: int,
    subject: str,
    query: str,
    grade: str = "",
) -> list[dict]:
    tokens = query.casefold().split()
    candidates = []
    for row in rows:
        try:
            meta = json.loads(row.get("recognition_meta_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if subject and meta.get("subject", "") != subject:
            continue
        if grade and str(meta.get("grade", "")).strip() != grade.strip():
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in ("question", "knowledge_point_id", "section_title", "tags")
        )
        if any(token not in text.casefold() for token in tokens):
            continue
        overdue = max(0, (now - row["due_at"]) / DAY)
        interval = max(1, row.get("interval_days") or 1)
        elapsed = max(0, (now - (row.get("last_review_at") or row["created_at"])) / DAY)
        # A deterministic urgency signal, deliberately not a calibrated recall probability.
        urgency = min(3, elapsed / interval)
        score = min(35, overdue * 6) + urgency * 8 + min(20, row.get("lapses", 0) * 6)
        reasons = []
        if row["due_at"] > now:
            reasons.append("Early practice requested")
        elif overdue >= 1:
            reasons.append("Overdue review")
        if row.get("lapses", 0) >= 2:
            reasons.append("Repeated mistakes")
        if row.get("learning_status") == "verify":
            score += 15
            reasons.append("Delayed mastery verification")
        if not row.get("review_count"):
            reasons.append("First independent attempt")
            score += min(8, max(0, (now - row["created_at"]) / DAY))
        if not reasons:
            reasons.append("Due for review")
        kind = row.get("question_type", "")
        minutes = 2 if kind in {"single_choice", "multi_choice", "true_false"} else 4
        if row.get("difficulty", "").lower() in {"hard", "difficult", "困难"}:
            minutes += 1
        candidates.append(
            {
                "entry_id": row["id"],
                "score": round(score, 3),
                "reasons": reasons,
                "estimated_minutes": minutes,
                "due_at": row["due_at"],
                "topic": row.get("knowledge_point_id")
                or row.get("section_title")
                or meta.get("subject")
                or str(row["id"]),
            }
        )
    result, used, topics = [], 0, {}
    while candidates and len(result) < limit:
        candidates.sort(
            key=lambda item: (
                -(item["score"] - 12 * topics.get(item["topic"], 0)),
                item["due_at"],
                item["entry_id"],
            )
        )
        item = candidates.pop(0)
        if used + item["estimated_minutes"] > budget_minutes:
            continue
        used += item["estimated_minutes"]
        item["score"] = round(item["score"] - 12 * topics.get(item["topic"], 0), 3)
        topics[item["topic"]] = topics.get(item["topic"], 0) + 1
        result.append({key: value for key, value in item.items() if key not in {"topic", "due_at"}})
    return result
