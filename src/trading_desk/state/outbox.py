from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading_desk.config import utc_now
from trading_desk.state.db import Database

BUZZ_WARNING_AFTER = timedelta(hours=24)
_RETRY_DELAYS = (
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
    index = min(max(attempt_count, 1) - 1, len(_RETRY_DELAYS) - 1)
    return _RETRY_DELAYS[index]


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


def claim_outbox_due(db: Database, *, now: datetime | None = None) -> list[OutboxClaim]:
    now = now or utc_now()
    claimed: list[OutboxClaim] = []
    with db.transaction() as txn:
        rows = txn.execute(
            """SELECT * FROM outbox
               WHERE status = 'PUBLISH_PENDING'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY outbox_id""",
            (now.isoformat(),),
        ).fetchall()
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
        aged = txn.execute(
            """SELECT * FROM outbox
               WHERE status = 'PUBLISH_PENDING'
                 AND topic != ?
                 AND created_at <= ?""",
            (_WARNING_TOPIC, (now - BUZZ_WARNING_AFTER).isoformat()),
        ).fetchall()
        for row in aged:
            _ensure_buzz_warning(db, txn, row, now=now)
    return claimed
