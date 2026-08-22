from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading_desk.config import UTC, utc_now
from trading_desk.state.approvals import ApprovalCommand, validate_approval
from trading_desk.state.db import BudgetKind, Database, RunIdentity

PAPER_STATES = frozenset(
    {
        "PAPER_RUNNING",
        "PAPER_DAILY_PAUSED",
        "PAPER_MDD_HALTED",
        "PAPER_DATA_GAP",
    }
)


class TransitionError(ValueError):
    """Fail-closed state-machine error. Nothing is committed."""


@dataclass(frozen=True, slots=True)
class _Rule:
    budget_kind: BudgetKind | tuple[BudgetKind, ...] | None = None
    approval_action: str | None = None
    requires_next_utc_day: bool = False
    requires_repaired: bool = False
    alert: bool = False


# Explicit spec §12 edges. Agents request; only this table is legal.
# Performance budget is consumed when gates evaluate (ANALYSIS_READY / OOS_RUNNING),
# honoring budget.technical_errors_consume_budget: false.
RULES: dict[tuple[str | None, str], _Rule] = {
    (None, "DRAFT"): _Rule(),
    ("DRAFT", "DEVELOPMENT_RUNNING"): _Rule(),
    ("DEVELOPMENT_RUNNING", "RUN_ERROR"): _Rule(),
    ("DEVELOPMENT_RUNNING", "DATA_BLOCKED"): _Rule(),
    ("RUN_ERROR", "DEVELOPMENT_RUNNING"): _Rule(),
    ("DATA_BLOCKED", "DEVELOPMENT_RUNNING"): _Rule(),
    ("DEVELOPMENT_RUNNING", "ANALYSIS_READY"): _Rule(budget_kind="performance"),
    ("ANALYSIS_READY", "MUTATION_PROPOSED"): _Rule(),
    ("DEVELOPMENT_RUNNING", "OOS_RUNNING"): _Rule(budget_kind=("performance", "oos")),
    ("OOS_RUNNING", "REJECTED"): _Rule(),
    ("OOS_RUNNING", "READY_FOR_PAPER"): _Rule(),
    ("READY_FOR_PAPER", "PAPER_RUNNING"): _Rule(approval_action="start_paper"),
    ("PAPER_RUNNING", "PAPER_DAILY_PAUSED"): _Rule(),
    ("PAPER_DAILY_PAUSED", "PAPER_RUNNING"): _Rule(requires_next_utc_day=True),
    ("PAPER_RUNNING", "PAPER_MDD_HALTED"): _Rule(alert=True),
    ("PAPER_MDD_HALTED", "PAPER_RUNNING"): _Rule(approval_action="resume_mdd"),
    ("PAPER_RUNNING", "PAPER_DATA_GAP"): _Rule(alert=True),
    ("PAPER_DATA_GAP", "PAPER_RUNNING"): _Rule(
        approval_action="resume_data_gap",
        requires_repaired=True,
    ),
}


def _current_state(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        """SELECT to_state FROM transitions
           WHERE run_id = ?
           ORDER BY transition_id DESC
           LIMIT 1""",
        (run_id,),
    ).fetchone()
    return None if row is None else str(row["to_state"])


def _load_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise TransitionError("unknown run_id")
    return row


def _consume_budget(
    db: Database,
    conn: sqlite3.Connection,
    *,
    family_id: str,
    kind: BudgetKind,
) -> None:
    row = conn.execute(
        "SELECT * FROM budgets WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    if row is None:
        raise TransitionError("unknown family budget")
    if kind == "performance":
        used, limit = row["performance_evaluated_versions"], row["max_performance_evaluated_versions"]
    else:
        used, limit = row["oos_evaluations"], row["max_oos_evaluations"]
    if int(used) >= int(limit):
        raise TransitionError("budget exhausted")
    try:
        db.consume_budget(conn, family_id=family_id, kind=kind)
    except sqlite3.IntegrityError as exc:
        raise TransitionError("budget exhausted") from exc


def _require_next_utc_day(conn: sqlite3.Connection, family_id: str, now: datetime) -> None:
    row = conn.execute(
        "SELECT status, payload_json FROM paper_state WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    if row is None or row["status"] != "PAPER_DAILY_PAUSED" or not row["payload_json"]:
        raise TransitionError("daily pause not expired")
    payload = json.loads(row["payload_json"])
    raw = payload.get("paused_at")
    try:
        paused_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise TransitionError("daily pause not expired") from exc
    if paused_at.tzinfo is None or paused_at.utcoffset() != timedelta(0):
        raise TransitionError("daily pause not expired")
    if now.astimezone(UTC).date() <= paused_at.astimezone(UTC).date():
        raise TransitionError("daily pause not expired")


def _merge_paper_payload(
    conn: sqlite3.Connection,
    family_id: str,
    extra: Mapping[str, Any],
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM paper_state WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    payload: dict[str, Any] = {}
    if row is not None and row["payload_json"]:
        loaded = json.loads(row["payload_json"])
        if isinstance(loaded, dict):
            payload = loaded
    payload.update(extra)
    return payload or None


def _paper_payload(
    conn: sqlite3.Connection,
    family_id: str,
    to_state: str,
    now: datetime,
) -> dict[str, Any] | None:
    extra: dict[str, Any]
    if to_state == "PAPER_DAILY_PAUSED":
        extra = {"paused_at": now.isoformat()}
    elif to_state == "PAPER_DATA_GAP":
        extra = {"repaired": "false"}
    elif to_state == "PAPER_RUNNING":
        extra = {"running_since": now.isoformat()}
    elif to_state in PAPER_STATES or to_state == "READY_FOR_PAPER":
        extra = {}
    else:
        return None
    return _merge_paper_payload(conn, family_id, extra)


def _enqueue_alert(
    db: Database,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    family_id: str,
    to_state: str,
    reason: str | None,
    idempotency_key: str,
    now: datetime,
) -> None:
    db.enqueue_outbox(
        topic="alert",
        payload={
            "event": to_state,
            "family_id": family_id,
            "reason": reason,
            "run_id": run_id,
        },
        idempotency_key=f"alert:{idempotency_key}",
        created_at=now.isoformat(),
        conn=conn,
    )


def transition(
    db: Database,
    *,
    run_id: str,
    to_state: str,
    idempotency_key: str,
    reason: str | None = None,
    approval: ApprovalCommand | None = None,
    allowlist: Collection[str] = (),
    now: datetime | None = None,
    repaired: bool = False,
) -> str:
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise TransitionError("now must be UTC")
    run_id = run_id.strip()
    to_state = to_state.strip()
    idempotency_key = idempotency_key.strip()
    if not run_id or not to_state or not idempotency_key:
        raise TransitionError("run_id, to_state, and idempotency_key are required")

    with db.transaction() as conn:
        run_row = _load_run(conn, run_id)
        current = _current_state(conn, run_id)
        existing = conn.execute(
            "SELECT * FROM transitions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["run_id"] == run_id and existing["to_state"] == to_state:
                return to_state
            raise TransitionError("idempotency conflict")

        rule = RULES.get((current, to_state))
        if rule is None:
            raise TransitionError("illegal transition")
        if rule.requires_repaired and not repaired:
            raise TransitionError("data gap not repaired")
        if rule.requires_next_utc_day:
            _require_next_utc_day(conn, run_row["family_id"], now)
        if rule.approval_action is not None:
            if approval is None:
                raise TransitionError("approval required")
            if approval.action != rule.approval_action:
                raise TransitionError("approval required")
            identity = RunIdentity(
                family_id=run_row["family_id"],
                strategy_version_id=run_row["strategy_version_id"],
                code_commit=run_row["code_commit"],
                data_snapshot_hash=run_row["data_snapshot_hash"],
                derived_data_hash=run_row["derived_data_hash"],
                validation_policy_hash=run_row["validation_policy_hash"],
                execution_policy_hash=run_row["execution_policy_hash"],
                run_id=run_row["run_id"],
            )
            validate_approval(
                db,
                approval,
                allowlist=allowlist,
                run=identity,
                now=now,
                conn=conn,
            )

        db.append_transition(
            conn,
            run_id=run_id,
            from_state=current,
            to_state=to_state,
            idempotency_key=idempotency_key,
            reason=reason,
        )
        kinds: tuple[BudgetKind, ...]
        raw = rule.budget_kind
        if raw is None:
            kinds = ()
        elif isinstance(raw, tuple):
            kinds = raw
        else:
            kinds = (raw,)
        for kind in kinds:
            _consume_budget(
                db,
                conn,
                family_id=run_row["family_id"],
                kind=kind,
            )
        if to_state in PAPER_STATES or to_state == "READY_FOR_PAPER":
            db.upsert_paper_state(
                family_id=run_row["family_id"],
                status=to_state,
                payload=_paper_payload(conn, run_row["family_id"], to_state, now),
                conn=conn,
            )
        if rule.alert:
            _enqueue_alert(
                db,
                conn,
                run_id=run_id,
                family_id=run_row["family_id"],
                to_state=to_state,
                reason=reason,
                idempotency_key=idempotency_key,
                now=now,
            )
    return to_state
