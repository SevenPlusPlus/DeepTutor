"""Durable inbound handoff and photo acknowledgements for personal WeChat.

The poll cursor covers a whole batch. Persisting every event in one transaction
before advancing it lets a later poll retry failed downloads without asking the
server to replay a cursor. Media credentials remain in this private channel DB.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable


class WeixinInbox:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "photo-inbox.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channel_inbox (
                    event_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    target TEXT NOT NULL, received_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS photo_outbox (
                    event_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, content TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
                    delivered INTEGER NOT NULL DEFAULT 0
                );
            """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(photo_outbox)")}
            if "account_id" not in columns:
                conn.execute(
                    "ALTER TABLE photo_outbox ADD COLUMN account_id TEXT NOT NULL DEFAULT ''"
                )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def event_key(partner_id: str, account_id: str, msg: dict) -> str:
        message_id = str(msg.get("message_id") or msg.get("seq") or "")
        if not message_id:
            # Retain timestamp + payload identity if the provider omits its id.
            stable = dict(msg)
            stable.pop("context_token", None)
            message_id = hashlib.sha256(
                json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        return hashlib.sha256(
            json.dumps([partner_id, account_id, msg.get("from_user_id"), message_id]).encode()
        ).hexdigest()

    def capture_batch(
        self,
        messages: list[dict],
        *,
        partner_id: str,
        account_id: str,
        freeze_target: Callable[[dict], dict],
        received_at: float | None = None,
    ) -> None:
        now = time.time() if received_at is None else received_at
        with self._connection() as conn:
            for msg in messages:
                event_key = self.event_key(partner_id, account_id, msg)
                if conn.execute(
                    "SELECT 1 FROM channel_inbox WHERE event_key=?", (event_key,)
                ).fetchone():
                    continue
                target = {**freeze_target(msg), "channel_account_id": account_id}
                conn.execute(
                    "INSERT INTO channel_inbox(event_key,payload,target,received_at) VALUES(?,?,?,?)",
                    (event_key, json.dumps(msg, ensure_ascii=False), json.dumps(target), now),
                )

    def pending(self, limit: int = 100, account_id: str | None = None) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM channel_inbox WHERE status='pending' AND next_retry_at<=? "
                "AND (? IS NULL OR json_extract(target,'$.channel_account_id')=? "
                "OR json_extract(target,'$.collect')=1) "
                "ORDER BY received_at,event_key LIMIT ?",
                (time.time(), account_id, account_id, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
                "target": json.loads(row["target"]),
            }
            for row in rows
        ]

    def complete(self, event_key: str, *, chat_id: str = "", reply: str = "") -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE channel_inbox SET status='processed',error='',payload='{}' WHERE event_key=?",
                (event_key,),
            )
            if reply:
                row = conn.execute(
                    "SELECT target FROM channel_inbox WHERE event_key=?", (event_key,)
                ).fetchone()
                account_id = json.loads(row[0]).get("channel_account_id", "")
                conn.execute(
                    "INSERT OR IGNORE INTO photo_outbox(event_key,chat_id,content,account_id) VALUES(?,?,?,?)",
                    (event_key, chat_id, reply, account_id),
                )

    def fail(self, event_key: str, *, permanent: bool = False) -> bool:
        # Errors are deliberately categorical: transport exceptions may contain
        # signed media URLs. Never copy those into a public status or a log.
        with self._connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM channel_inbox WHERE event_key=?", (event_key,)
            ).fetchone()
            attempts = int(row[0]) + 1
            terminal = permanent or attempts >= 8
            conn.execute(
                "UPDATE channel_inbox SET status=?,attempts=?,next_retry_at=?,error=? WHERE event_key=?",
                (
                    "failed" if terminal else "pending",
                    attempts,
                    time.time() + min(2**attempts, 300),
                    "photo_processing_failed",
                    event_key,
                ),
            )
        return terminal

    def enqueue_notification(self, event_key: str, chat_id: str, content: str) -> None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT target FROM channel_inbox WHERE event_key=?", (event_key,)
            ).fetchone()
            account_id = json.loads(row[0]).get("channel_account_id", "")
            conn.execute(
                "INSERT OR IGNORE INTO photo_outbox(event_key,chat_id,content,account_id) VALUES(?,?,?,?)",
                (event_key, chat_id, content, account_id),
            )

    def notifications(self, account_id: str | None = None) -> list[dict]:
        with self._connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM photo_outbox WHERE delivered=0 AND next_retry_at<=? "
                    "AND (? IS NULL OR account_id=?) ORDER BY rowid LIMIT 100",
                    (time.time(), account_id, account_id),
                )
            ]

    def notification_result(self, event_key: str, delivered: bool) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE photo_outbox SET delivered=?,attempts=attempts+1,next_retry_at=? WHERE event_key=?",
                (int(delivered), time.time() + 30, event_key),
            )

    def photo_scopes(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT DISTINCT target FROM channel_inbox").fetchall()
        return [
            target
            for row in rows
            if (target := json.loads(row[0])).get("user_id") and target.get("collect")
        ]
