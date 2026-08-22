from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading_desk.config import utc_now
from trading_desk.state.db import Database

BUZZ_WARNING_AFTER = timedelta(hours=24)
RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
    timedelta(hours=6),
)
_WARNING_TOPIC = "buzz_warning"


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    outbox_id: int
    topic: str
    payload: dict[str, Any]
    idempotency_key: str
    attempt_count: int
    next_attempt_at: str
    created_at: str


def _backoff(attempt_count: int) -> timedelta:
    index = min(max(attempt_count, 1) - 1, len(RETRY_DELAYS) - 1)
    return RETRY_DELAYS[index]


def _warning_key(idempotency_key: str) -> str:
    return f"{idempotency_key}::buzz-warning-24h"


def _ensure_buzz_warning(
    db: Database,
    txn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime,
) -> None:
    if row["topic"] == _WARNING_TOPIC:
        return
    created = datetime.fromisoformat(str(row["created_at"]))
    if now - created < BUZZ_WARNING_AFTER:
        return
    key = _warning_key(str(row["idempotency_key"]))
    existing = txn.execute(
        "SELECT 1 FROM outbox WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if existing is not None:
        return
    try:
        db.enqueue_outbox(
            topic=_WARNING_TOPIC,
            payload={
                "event": "OUTBOX_RETRY_EXCEEDED_24H",
                "outbox_id": int(row["outbox_id"]),
                "source_idempotency_key": row["idempotency_key"],
            },
            idempotency_key=key,
            created_at=now.isoformat(),
            conn=txn,
        )
    except sqlite3.IntegrityError:
        return


def claim_outbox_due(
    db: Database,
    *,
    now: datetime | None = None,
    topic: str | None = None,
) -> list[OutboxClaim]:
    now = now or utc_now()
    claimed: list[OutboxClaim] = []
    with db.transaction() as txn:
        sql = """SELECT * FROM outbox
                 WHERE status = 'PUBLISH_PENDING'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"""
        params: list[Any] = [now.isoformat()]
        if topic is not None:
            sql += " AND topic = ?"
            params.append(topic)
        sql += " ORDER BY outbox_id"
        rows = txn.execute(sql, params).fetchall()
        for row in rows:
            attempt_count = int(row["attempt_count"]) + 1
            next_attempt_at = (now + _backoff(attempt_count)).isoformat()
            txn.execute(
                """UPDATE outbox
                   SET attempt_count = ?, next_attempt_at = ?
                   WHERE outbox_id = ?""",
                (attempt_count, next_attempt_at, row["outbox_id"]),
            )
            claimed.append(
                OutboxClaim(
                    outbox_id=int(row["outbox_id"]),
                    topic=str(row["topic"]),
                    payload=json.loads(row["payload_json"]),
                    idempotency_key=str(row["idempotency_key"]),
                    attempt_count=attempt_count,
                    next_attempt_at=next_attempt_at,
                    created_at=str(row["created_at"]),
                )
            )
            _ensure_buzz_warning(db, txn, row, now=now)
        aged_sql = """SELECT * FROM outbox
                      WHERE status = 'PUBLISH_PENDING'
                        AND topic != ?
                        AND created_at <= ?"""
        aged_params: list[Any] = [_WARNING_TOPIC, (now - BUZZ_WARNING_AFTER).isoformat()]
        if topic is not None:
            aged_sql += " AND topic = ?"
            aged_params.append(topic)
        aged = txn.execute(aged_sql, aged_params).fetchall()
        for row in aged:
            _ensure_buzz_warning(db, txn, row, now=now)
    return claimed


def mark_outbox_published(
    db: Database,
    *,
    outbox_id: int,
    published_revision_id: str,
) -> None:
    if not isinstance(published_revision_id, str) or not published_revision_id.strip():
        raise ValueError("published_revision_id is required")
    with db.transaction() as txn:
        row = txn.execute(
            "SELECT status, published_revision_id FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown outbox_id")
        existing = row["published_revision_id"]
        if existing and existing != published_revision_id:
            raise ValueError("published_revision_id is immutable")
        if row["status"] == "PUBLISHED" and existing == published_revision_id:
            return
        txn.execute(
            """UPDATE outbox
               SET status = 'PUBLISHED', published_revision_id = ?
               WHERE outbox_id = ?""",
            (published_revision_id, outbox_id),
        )
