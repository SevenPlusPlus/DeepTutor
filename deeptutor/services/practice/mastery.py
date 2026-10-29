"""Evidence-based mistake closure, independent of the interval scheduler.

The thresholds below are versioned MVP policy, not a universal learning model.
Legacy review events are intentionally not upgraded into objective evidence.
"""

from __future__ import annotations

import json
import sqlite3

from .scheduler import DAY

POLICY_VERSION = "mistake-mastery-v1"


def initialize_mastery(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(practice_review_state)")}
    for name, declaration in (
        ("learning_status", "TEXT NOT NULL DEFAULT 'open'"),
        ("paused", "INTEGER NOT NULL DEFAULT 0"),
        ("closed_at", "REAL"),
        ("evidence_since", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE practice_review_state ADD COLUMN {name} {declaration}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS practice_mastery_attempts (
            request_id TEXT PRIMARY KEY REFERENCES practice_review_events(request_id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            target_entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            correct INTEGER,
            independent INTEGER NOT NULL DEFAULT 0,
            contributes INTEGER NOT NULL DEFAULT 1,
            reviewed_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_mastery_attempt_target
            ON practice_mastery_attempts(target_entry_id, reviewed_at);
        CREATE TABLE IF NOT EXISTS practice_exposures (
            request_id TEXT PRIMARY KEY,
            entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            target_entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            exposed_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mastery_exposure_target
            ON practice_exposures(target_entry_id, exposed_at);
        CREATE TABLE IF NOT EXISTS practice_mastery_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            decided_at REAL NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            policy_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mastery_decisions_entry
            ON practice_mastery_decisions(entry_id, decided_at);
    """
    )


def target_for_entry(conn: sqlite3.Connection, entry_id: int) -> tuple[int, str | None, bool]:
    relation = conn.execute(
        "SELECT * FROM practice_variants WHERE entry_id=? AND validation_status='approved'",
        (entry_id,),
    ).fetchone()
    if relation is None:
        return entry_id, None, True
    return relation["parent_entry_id"], relation["kind"], not bool(relation["introduces_new_skill"])


def record_exposure(
    conn: sqlite3.Connection, entry_id: int, request_id: str, kind: str, now: float
) -> dict:
    """Write before showing help, including when the learner never submits."""
    target, _, same_skill = target_for_entry(conn, entry_id)
    target = target if same_skill else entry_id
    previous = conn.execute(
        "SELECT * FROM practice_exposures WHERE request_id=?", (request_id,)
    ).fetchone()
    if previous:
        if previous["entry_id"] != entry_id or previous["kind"] != kind:
            raise ValueError("This exposure ID was already used for different help")
        return {"entry_id": entry_id, "kind": kind, "exposed_at": previous["exposed_at"]}
    conn.execute(
        "INSERT INTO practice_exposures VALUES(?,?,?,?,?)",
        (request_id, entry_id, target, kind, now),
    )
    return {"entry_id": entry_id, "kind": kind, "exposed_at": now}


def recently_exposed(conn: sqlite3.Connection, entry_id: int, target_id: int, now: float) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM practice_exposures WHERE exposed_at>? AND exposed_at<=? "
            "AND (target_entry_id=? OR entry_id=?) LIMIT 1",
            (now - DAY, now, target_id, entry_id),
        ).fetchone()
        is not None
    )


def mastery_evidence(conn: sqlite3.Connection, entry_id: int, *, frozen: bool = True) -> dict:
    state = conn.execute(
        "SELECT * FROM practice_review_state WHERE entry_id=?", (entry_id,)
    ).fetchone()
    if frozen and state and state["learning_status"] == "closed":
        decision = conn.execute(
            "SELECT evidence_json FROM practice_mastery_decisions WHERE entry_id=? "
            "AND action='closed' ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        if decision:
            return json.loads(decision[0])
    since = state["evidence_since"] if state else 0
    rows = conn.execute(
        "SELECT * FROM practice_mastery_attempts WHERE target_entry_id=? "
        "AND contributes=1 AND reviewed_at>=? ORDER BY reviewed_at, rowid",
        (entry_id, since),
    ).fetchall()
    last_failure = max((i for i, row in enumerate(rows) if row["correct"] == 0), default=-1)
    successes = [
        row for row in rows[last_failure + 1 :] if row["correct"] == 1 and row["independent"]
    ]
    originals = [row for row in successes if row["kind"] in {"original", "spotcheck"}]
    spaced_originals = []
    for row in originals:
        if not spaced_originals or row["reviewed_at"] - spaced_originals[-1]["reviewed_at"] >= DAY:
            spaced_originals.append(row)
    spaced = len(spaced_originals) >= 2
    transfer = any(
        row["entry_id"] != entry_id and row["kind"] in {"similar", "advanced"} for row in successes
    )
    delayed = bool(
        spaced
        and len(successes) >= 2
        and successes[-1]["kind"] in {"original", "spotcheck"}
        and successes[-1]["reviewed_at"] - successes[-2]["reviewed_at"] >= 7 * DAY
    )
    missing = [
        name
        for name, done in (("spaced", spaced), ("transfer", transfer), ("delayed", delayed))
        if not done
    ]
    return {
        "spaced": spaced,
        "transfer": transfer,
        "delayed": delayed,
        "ready": not missing,
        "independent_successes": len(spaced_originals),
        "attempt_ids": [row["request_id"] for row in successes],
        "missing": missing,
        "policy_version": POLICY_VERSION,
    }


def apply_mastery(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    correct: bool | None,
    independent: bool,
    now: float,
    contributes: bool = True,
) -> dict:
    state = dict(
        conn.execute("SELECT * FROM practice_review_state WHERE entry_id=?", (entry_id,)).fetchone()
    )
    before = state["learning_status"]
    evidence = mastery_evidence(conn, entry_id)
    after = before
    reason = ""
    if contributes and correct is False:
        after, reason = "open", "confirmed_same_skill_failure"
        conn.execute(
            "UPDATE practice_review_state SET is_mistake=1,due_at=?,closed_at=NULL WHERE entry_id=?",
            (now + DAY, entry_id),
        )
        conn.execute("UPDATE notebook_entries SET resolved=0 WHERE id=?", (entry_id,))
        evidence = mastery_evidence(conn, entry_id, frozen=False)
    elif contributes and correct is True and independent and before != "closed":
        if evidence["ready"]:
            after, reason = "closed", "spaced_transfer_delayed_passed"
            conn.execute(
                "UPDATE practice_review_state SET closed_at=?,due_at=? WHERE entry_id=?",
                (now, now + 21 * DAY, entry_id),
            )
        else:
            after = "verify"
            if evidence["transfer"]:
                conn.execute(
                    "UPDATE practice_review_state SET due_at=MAX(due_at,?) WHERE entry_id=?",
                    (now + 7 * DAY, entry_id),
                )
    # Successful/assisted early spot-checks never retract an existing decision.
    if before == "closed" and after == "closed":
        conn.execute(
            "UPDATE practice_review_state SET due_at=? WHERE entry_id=?", (now + 21 * DAY, entry_id)
        )
    conn.execute(
        "UPDATE practice_review_state SET learning_status=? WHERE entry_id=?", (after, entry_id)
    )
    closed, reopened = (
        before != "closed" and after == "closed",
        before == "closed" and after == "open",
    )
    if closed or reopened:
        conn.execute(
            "INSERT INTO practice_mastery_decisions(entry_id,action,decided_at,reason,evidence_json,policy_version) VALUES(?,?,?,?,?,?)",
            (
                entry_id,
                "closed" if closed else "reopened",
                now,
                reason,
                json.dumps(evidence),
                POLICY_VERSION,
            ),
        )
    return {"learning_status": after, "evidence": evidence, "closed": closed, "reopened": reopened}
