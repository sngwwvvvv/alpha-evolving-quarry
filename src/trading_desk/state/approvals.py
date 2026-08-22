from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_desk.config import canonical_json, sha256_hex, utc_now
from trading_desk.state.db import Database, RunIdentity

APPROVAL_MAX_AGE = timedelta(hours=24)
START_PAPER = "start_paper"
RESUME_MDD = "resume_mdd"
RESUME_DATA_GAP = "resume_data_gap"
APPROVAL_ACTIONS = frozenset({START_PAPER, RESUME_MDD, RESUME_DATA_GAP})
OBJECT_HASH_KEYS = frozenset(
    {
        "code_commit",
        "data_snapshot_hash",
        "derived_data_hash",
        "execution_policy_hash",
        "family_id",
        "run_id",
        "strategy_version_id",
        "validation_policy_hash",
    }
)


class ApprovalError(ValueError):
    """Fail-closed approval validation error."""


@dataclass(frozen=True, slots=True)
class ApprovalCommand:
    actor_id: str
    action: str
    object_hashes: Mapping[str, str]
    source_command_hash: str
    timestamp: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_hashes", dict(self.object_hashes))


def hash_approval_command(
    *,
    actor_id: str,
    action: str,
    object_hashes: Mapping[str, str],
    timestamp: datetime,
    idempotency_key: str,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "action": action,
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
                "object_hashes": dict(object_hashes),
                "timestamp": timestamp.isoformat(),
            }
        )
    )


def _require_utc_timestamp(value: datetime, *, now: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ApprovalError("timestamp must be UTC")
    if value > now:
        raise ApprovalError("timestamp is in the future")
    if now - value >= APPROVAL_MAX_AGE:
        raise ApprovalError("stale approval")


def _expected_hashes(run: RunIdentity) -> dict[str, str]:
    return {
        "code_commit": run.code_commit,
        "data_snapshot_hash": run.data_snapshot_hash,
        "derived_data_hash": run.derived_data_hash,
        "execution_policy_hash": run.execution_policy_hash,
        "family_id": run.family_id,
        "run_id": run.run_id,
        "strategy_version_id": run.strategy_version_id,
        "validation_policy_hash": run.validation_policy_hash,
    }


def _reject_ambiguous(command: ApprovalCommand) -> None:
    if command.action not in APPROVAL_ACTIONS:
        raise ApprovalError("ambiguous approval")
    hashes = command.object_hashes
    if set(hashes) != OBJECT_HASH_KEYS:
        raise ApprovalError("ambiguous approval")
    for key, value in hashes.items():
        if not isinstance(value, str) or not value.strip():
            raise ApprovalError("ambiguous approval")


def _reject_superseded(txn: sqlite3.Connection, run: RunIdentity) -> None:
    named = txn.execute(
        """SELECT created_at, rowid AS rid
           FROM strategy_versions
           WHERE strategy_version_id = ?""",
        (run.strategy_version_id,),
    ).fetchone()
    if named is None:
        raise ApprovalError("object hash mismatch")
    newer = txn.execute(
        """SELECT 1 FROM strategy_versions
           WHERE family_id = ?
             AND (created_at > ? OR (created_at = ? AND rowid > ?))
           LIMIT 1""",
        (run.family_id, named["created_at"], named["created_at"], named["rid"]),
    ).fetchone()
    if newer is not None:
        raise ApprovalError("superseded version")


def _payload_matches(row: sqlite3.Row, command: ApprovalCommand) -> bool:
    return (
        row["actor_id"] == command.actor_id
        and row["action"] == command.action
        and row["source_command_hash"] == command.source_command_hash
        and row["object_hashes_json"] == canonical_json(dict(command.object_hashes))
    )


def validate_approval(
    db: Database,
    command: ApprovalCommand,
    *,
    allowlist: Collection[str],
    run: RunIdentity,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ApprovalError("timestamp must be UTC")
    _require_utc_timestamp(command.timestamp, now=now)
    if command.actor_id not in allowlist:
        raise ApprovalError("unallowlisted actor")
    _reject_ambiguous(command)
    expected_hash = hash_approval_command(
        actor_id=command.actor_id,
        action=command.action,
        object_hashes=command.object_hashes,
        timestamp=command.timestamp,
        idempotency_key=command.idempotency_key,
    )
    if command.source_command_hash != expected_hash:
        raise ApprovalError("invalid source_command_hash")
    expected = _expected_hashes(run)
    for key, value in expected.items():
        if command.object_hashes.get(key) != value:
            raise ApprovalError("object hash mismatch")

    def _commit(txn: sqlite3.Connection) -> str:
        _reject_superseded(txn, run)
        existing = db.get_approval(command.idempotency_key, conn=txn)
        if existing is not None:
            if _payload_matches(existing, command):
                return str(existing["approval_id"])
            raise ApprovalError("idempotency conflict")
        return db.record_approval(
            actor_id=command.actor_id,
            action=command.action,
            object_hashes=dict(command.object_hashes),
            source_command_hash=command.source_command_hash,
            idempotency_key=command.idempotency_key,
            conn=txn,
        )

    if conn is not None:
        return _commit(conn)
    with db.transaction() as txn:
        return _commit(txn)
