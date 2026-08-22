from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading_desk.config import UTC, sha256_hex
from trading_desk.state.approvals import (
    APPROVAL_MAX_AGE,
    APPROVE_NEW_FAMILY,
    ApprovalCommand,
    ApprovalError,
    approve_new_family,
    hash_approval_command,
    validate_approval,
)
from trading_desk.state.db import Database, RunIdentity
from trading_desk.state.outbox import BUZZ_WARNING_AFTER, claim_outbox_due
from trading_desk.state.transitions import TransitionError, transition

COMMIT = "c" * 40
ALLOWLIST = frozenset({"user-1"})
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _digest(label: str) -> str:
    return sha256_hex(label)


def _run_hashes() -> dict[str, str]:
    return {
        "data_snapshot_hash": _digest("snapshot"),
        "derived_data_hash": _digest("derived"),
        "validation_policy_hash": _digest("validation-policy-v2"),
        "execution_policy_hash": _digest("execution"),
    }


def _register_run(db: Database, *, family_id: str | None = None) -> tuple[str, str, RunIdentity]:
    family_id = family_id or db.create_family()
    version_id = db.register_version(
        family_id,
        code_commit=COMMIT,
        spec={"lookback": 20, "threshold": 1.5, "nonce": version_nonce(family_id)},
    )
    run = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        **_run_hashes(),
    )
    return family_id, version_id, run


_NONCES: dict[str, int] = {}


def version_nonce(family_id: str) -> int:
    _NONCES[family_id] = _NONCES.get(family_id, 0) + 1
    return _NONCES[family_id]


def object_hashes(run: RunIdentity) -> dict[str, str]:
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


def make_approval(
    run: RunIdentity,
    *,
    action: str,
    actor_id: str = "user-1",
    idempotency_key: str = "approve-1",
    timestamp: datetime = NOW,
    hashes: dict[str, str] | None = None,
    source_command_hash: str | None = None,
) -> ApprovalCommand:
    hashes = hashes if hashes is not None else object_hashes(run)
    digest = source_command_hash or hash_approval_command(
        actor_id=actor_id,
        action=action,
        object_hashes=hashes,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )
    return ApprovalCommand(
        actor_id=actor_id,
        action=action,
        object_hashes=hashes,
        source_command_hash=digest,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )


def apply(
    db: Database,
    run: RunIdentity,
    to_state: str,
    *,
    seq: int,
    now: datetime = NOW,
    **kwargs: object,
) -> str:
    return transition(
        db,
        run_id=run.run_id,
        to_state=to_state,
        idempotency_key=f"{run.run_id}:{seq}:{to_state}",
        now=now,
        **kwargs,
    )


def walk(db: Database, run: RunIdentity, states: tuple[str, ...], *, now: datetime = NOW) -> None:
    for seq, state in enumerate(states):
        apply(db, run, state, seq=seq, now=now)


def states_of(db: Database, run: RunIdentity) -> list[str]:
    return [row["to_state"] for row in db.list_transitions(run.run_id)]


def outbox_rows(db: Database) -> list[dict[str, object]]:
    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM outbox ORDER BY outbox_id"))
    finally:
        conn.close()
    return [
        {
            "topic": row["topic"],
            "payload": json.loads(row["payload_json"]),
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "next_attempt_at": row["next_attempt_at"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def paper_path() -> tuple[str, ...]:
    return (
        "DRAFT",
        "DEVELOPMENT_RUNNING",
        "OOS_RUNNING",
        "READY_FOR_PAPER",
        "PAPER_RUNNING",
    )


def go_paper(
    db: Database,
    run: RunIdentity,
    *,
    now: datetime = NOW,
    allowlist: frozenset[str] = ALLOWLIST,
) -> None:
    walk(db, run, paper_path()[:-1], now=now)
    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=len(paper_path()) - 1,
        now=now,
        approval=make_approval(run, action="start_paper"),
        allowlist=allowlist,
    )


def test_technical_rerun_does_not_consume_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, run = _register_run(db)
    walk(db, run, ("DRAFT", "DEVELOPMENT_RUNNING"))
    assert db.get_budget(family_id).performance_evaluated_versions == 1

    apply(db, run, "RUN_ERROR", seq=2, reason="engine crash")
    apply(db, run, "DEVELOPMENT_RUNNING", seq=3, reason="technical rerun")
    apply(db, run, "DATA_BLOCKED", seq=4, reason="missing bars")
    apply(db, run, "DEVELOPMENT_RUNNING", seq=5, reason="technical rerun")

    assert db.get_budget(family_id).performance_evaluated_versions == 1
    assert db.get_budget(family_id).oos_evaluations == 0
    assert states_of(db, run) == [
        "DRAFT",
        "DEVELOPMENT_RUNNING",
        "RUN_ERROR",
        "DEVELOPMENT_RUNNING",
        "DATA_BLOCKED",
        "DEVELOPMENT_RUNNING",
    ]

    apply(db, run, "DEVELOPMENT_RUNNING", seq=5, reason="technical rerun")
    assert states_of(db, run).count("DEVELOPMENT_RUNNING") == 3


def test_development_mutation_path_uses_a_new_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, run = _register_run(db)
    walk(
        db,
        run,
        ("DRAFT", "DEVELOPMENT_RUNNING", "ANALYSIS_READY", "MUTATION_PROPOSED"),
    )
    with pytest.raises(TransitionError, match="illegal transition"):
        apply(db, run, "DEVELOPMENT_RUNNING", seq=99)

    _, _, successor = _register_run(db, family_id=family_id)
    walk(db, successor, ("DRAFT", "DEVELOPMENT_RUNNING"))

    assert states_of(db, run)[-1] == "MUTATION_PROPOSED"
    assert states_of(db, successor) == ["DRAFT", "DEVELOPMENT_RUNNING"]
    assert db.get_budget(family_id).performance_evaluated_versions == 2


def test_oos_terminal_paths(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, rejected = _register_run(db)
    walk(db, rejected, ("DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING", "REJECTED"))
    assert db.get_budget(family_id).oos_evaluations == 1
    for nxt in ("READY_FOR_PAPER", "PAPER_RUNNING", "OOS_RUNNING", "ANALYSIS_READY"):
        with pytest.raises(TransitionError, match="illegal transition"):
            apply(db, rejected, nxt, seq=90)

    _, _, passed = _register_run(db, family_id=family_id)
    walk(db, passed, ("DRAFT", "DEVELOPMENT_RUNNING"))
    with pytest.raises(TransitionError, match="budget exhausted"):
        apply(db, passed, "OOS_RUNNING", seq=2)
    assert states_of(db, passed) == ["DRAFT", "DEVELOPMENT_RUNNING"]

    successor_id = approve_new_family(
        db,
        make_new_family_approval(
            rejected_family_id=family_id,
            proposed_family_id="oos-retry-family",
        ),
        allowlist=ALLOWLIST,
        now=NOW,
    )
    other_family, _, other = _register_run(db, family_id=successor_id)
    walk(db, other, ("DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING", "READY_FOR_PAPER"))
    assert db.get_budget(other_family).oos_evaluations == 1
    with pytest.raises(TransitionError, match="approval required"):
        apply(db, other, "PAPER_RUNNING", seq=4)
    assert states_of(db, other)[-1] == "READY_FOR_PAPER"


def test_paper_daily_pause_resumes_next_utc_day_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    go_paper(db, run, now=NOW)
    apply(db, run, "PAPER_DAILY_PAUSED", seq=10, now=NOW, reason="daily_loss")
    assert db.get_paper_state(run.family_id)["status"] == "PAPER_DAILY_PAUSED"

    with pytest.raises(TransitionError, match="daily pause not expired"):
        apply(db, run, "PAPER_RUNNING", seq=11, now=NOW.replace(hour=23, minute=59))

    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=12,
        now=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
        reason="daily_resume",
    )
    assert states_of(db, run)[-1] == "PAPER_RUNNING"
    assert db.get_paper_state(run.family_id)["status"] == "PAPER_RUNNING"


def test_mdd_halt_requires_explicit_approval(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    go_paper(db, run)
    apply(db, run, "PAPER_MDD_HALTED", seq=10, reason="mdd_stop")
    with pytest.raises(TransitionError, match="approval required"):
        apply(db, run, "PAPER_RUNNING", seq=11)
    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=12,
        approval=make_approval(run, action="resume_mdd", idempotency_key="resume-mdd"),
        allowlist=ALLOWLIST,
    )
    assert states_of(db, run)[-1] == "PAPER_RUNNING"
    alerts = [row for row in outbox_rows(db) if row["payload"]["event"] == "PAPER_MDD_HALTED"]
    assert len(alerts) == 1


def test_data_gap_halt_requires_repair_and_approval(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    go_paper(db, run)
    apply(db, run, "PAPER_DATA_GAP", seq=10, reason="unreconciled_gap")
    approval = make_approval(run, action="resume_data_gap", idempotency_key="resume-gap")
    with pytest.raises(TransitionError, match="data gap not repaired"):
        apply(db, run, "PAPER_RUNNING", seq=11, approval=approval, allowlist=ALLOWLIST)
    with pytest.raises(TransitionError, match="approval required"):
        apply(db, run, "PAPER_RUNNING", seq=12, repaired=True)
    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=13,
        approval=approval,
        allowlist=ALLOWLIST,
        repaired=True,
    )
    assert states_of(db, run)[-1] == "PAPER_RUNNING"


def test_illegal_and_unvalidated_requests_do_not_commit(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, run = _register_run(db)
    walk(db, run, ("DRAFT",))
    with pytest.raises(TransitionError, match="illegal transition"):
        apply(db, run, "READY_FOR_PAPER", seq=1)
    with pytest.raises(TransitionError, match="illegal transition"):
        apply(db, run, "PAPER_RUNNING", seq=2)
    with pytest.raises(TransitionError, match="illegal transition"):
        apply(db, run, "NOT_A_STATE", seq=3)
    assert states_of(db, run) == ["DRAFT"]
    assert db.get_budget(family_id).performance_evaluated_versions == 0
    assert outbox_rows(db) == []


def test_approval_allowlist_hashes_timestamp_and_source_command(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    walk(db, run, paper_path()[:-1])

    with pytest.raises(ApprovalError, match="unallowlisted actor"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", actor_id="intruder"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="object hash mismatch"):
        validate_approval(
            db,
            make_approval(
                run,
                action="start_paper",
                hashes={**object_hashes(run), "execution_policy_hash": _digest("other")},
            ),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="invalid source_command_hash"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", source_command_hash=_digest("tampered")),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    naive = datetime(2026, 8, 23, 12, 0)
    with pytest.raises(ApprovalError, match="timestamp"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", timestamp=naive),  # type: ignore[arg-type]
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    future = make_approval(run, action="start_paper", timestamp=NOW + timedelta(seconds=1))
    with pytest.raises(ApprovalError, match="timestamp"):
        validate_approval(db, future, allowlist=ALLOWLIST, run=run, now=NOW)

    approval_id = validate_approval(
        db,
        make_approval(run, action="start_paper"),
        allowlist=ALLOWLIST,
        run=run,
        now=NOW,
    )
    assert approval_id
    replay = validate_approval(
        db,
        make_approval(run, action="start_paper"),
        allowlist=ALLOWLIST,
        run=run,
        now=NOW,
    )
    assert replay == approval_id


def test_approval_rejects_stale_superseded_ambiguous_and_conflicting_idempotency(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, run = _register_run(db)
    walk(db, run, paper_path()[:-1])

    stale = make_approval(
        run,
        action="start_paper",
        timestamp=NOW - APPROVAL_MAX_AGE,
        idempotency_key="stale",
    )
    with pytest.raises(ApprovalError, match="stale approval"):
        validate_approval(db, stale, allowlist=ALLOWLIST, run=run, now=NOW)

    validate_approval(
        db,
        make_approval(run, action="start_paper", idempotency_key="before-version"),
        allowlist=ALLOWLIST,
        run=run,
        now=NOW,
    )
    validate_approval(
        db,
        make_approval(run, action="start_paper", idempotency_key="dup"),
        allowlist=ALLOWLIST,
        run=run,
        now=NOW,
    )
    with pytest.raises(ApprovalError, match="idempotency conflict"):
        validate_approval(
            db,
            make_approval(run, action="resume_mdd", idempotency_key="dup"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )

    missing = dict(object_hashes(run))
    missing.pop("validation_policy_hash")
    with pytest.raises(ApprovalError, match="ambiguous approval"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", hashes=missing, idempotency_key="missing"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    extra = {**object_hashes(run), "maybe_run_id": run.run_id}
    with pytest.raises(ApprovalError, match="ambiguous approval"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", hashes=extra, idempotency_key="extra"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="ambiguous approval"):
        validate_approval(
            db,
            make_approval(run, action="not-an-action", idempotency_key="bad-action"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )

    _register_run(db, family_id=family_id)
    with pytest.raises(ApprovalError, match="superseded"):
        validate_approval(
            db,
            make_approval(run, action="start_paper", idempotency_key="after-version"),
            allowlist=ALLOWLIST,
            run=run,
            now=NOW,
        )


def test_ready_for_paper_starts_only_with_exact_approval(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    walk(db, run, paper_path()[:-1])
    with pytest.raises(TransitionError, match="approval required"):
        apply(db, run, "PAPER_RUNNING", seq=4)
    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=5,
        approval=make_approval(run, action="start_paper"),
        allowlist=ALLOWLIST,
    )
    assert states_of(db, run)[-1] == "PAPER_RUNNING"
    assert db.get_paper_state(run.family_id)["status"] == "PAPER_RUNNING"


def test_transition_and_budget_and_outbox_roll_back_together(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _, run = _register_run(db)
    walk(db, run, ("DRAFT", "DEVELOPMENT_RUNNING"))
    budget = db.get_budget(family_id)
    remaining = budget.max_performance_evaluated_versions - budget.performance_evaluated_versions
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for _ in range(remaining):
            db.consume_budget(conn, family_id=family_id, kind="performance")
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert (
        db.get_budget(family_id).performance_evaluated_versions
        == budget.max_performance_evaluated_versions
    )

    _, _, other = _register_run(db, family_id=family_id)
    walk(db, other, ("DRAFT",))
    with pytest.raises(TransitionError, match="budget exhausted"):
        apply(db, other, "DEVELOPMENT_RUNNING", seq=1)
    assert states_of(db, other) == ["DRAFT"]
    assert (
        db.get_budget(family_id).performance_evaluated_versions
        == budget.max_performance_evaluated_versions
    )


def test_deterministic_outbox_alerts_and_claim_backoff_without_llm(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    _, _, run = _register_run(db)
    go_paper(db, run, now=NOW)
    apply(db, run, "PAPER_DATA_GAP", seq=20, now=NOW, reason="unreconciled_gap")

    rows = outbox_rows(db)
    assert len(rows) == 1
    assert rows[0]["topic"] == "alert"
    assert rows[0]["payload"] == {
        "event": "PAPER_DATA_GAP",
        "family_id": run.family_id,
        "reason": "unreconciled_gap",
        "run_id": run.run_id,
    }
    assert rows[0]["status"] == "PUBLISH_PENDING"
    assert rows[0]["attempt_count"] == 0

    first = claim_outbox_due(db, now=NOW)
    assert len(first) == 1
    assert first[0].attempt_count == 1
    assert first[0].payload["event"] == "PAPER_DATA_GAP"
    assert claim_outbox_due(db, now=NOW) == []

    second = claim_outbox_due(db, now=NOW + timedelta(minutes=5))
    assert len(second) == 1
    assert second[0].attempt_count == 2
    third = claim_outbox_due(db, now=NOW + timedelta(minutes=5 + 15))
    assert third[0].attempt_count == 3
    fourth = claim_outbox_due(db, now=NOW + timedelta(minutes=5 + 15 + 60))
    assert fourth[0].attempt_count == 4
    assert claim_outbox_due(db, now=NOW + timedelta(minutes=5 + 15 + 60 + 5)) == []
    later = claim_outbox_due(db, now=NOW + timedelta(minutes=5 + 15 + 60) + timedelta(hours=6))
    assert later[0].attempt_count == 5

    created = datetime.fromisoformat(str(outbox_rows(db)[0]["created_at"]))
    warning_time = created + BUZZ_WARNING_AFTER
    claim_outbox_due(db, now=warning_time)
    warnings = [row for row in outbox_rows(db) if row["topic"] == "buzz_warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["event"] == "OUTBOX_RETRY_EXCEEDED_24H"
    assert warnings[0]["payload"]["source_idempotency_key"] == rows[0]["idempotency_key"]
    claim_outbox_due(db, now=warning_time)
    assert len([row for row in outbox_rows(db) if row["topic"] == "buzz_warning"]) == 1


def test_state_modules_have_no_llm_imports() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "trading_desk" / "state"
    forbidden = ("openai", "anthropic", "litellm", "hermes", "trading_desk.agents")
    for name in ("transitions.py", "approvals.py", "outbox.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            item == needle or item.startswith(needle + ".")
            for item in imported
            for needle in forbidden
        )


def make_new_family_approval(
    *,
    rejected_family_id: str,
    proposed_family_id: str,
    actor_id: str = "user-1",
    idempotency_key: str = "new-family-1",
    timestamp: datetime = NOW,
    hashes: dict[str, str] | None = None,
) -> ApprovalCommand:
    hashes = hashes if hashes is not None else {
        "proposed_family_id": proposed_family_id,
        "rejected_family_id": rejected_family_id,
    }
    digest = hash_approval_command(
        actor_id=actor_id,
        action=APPROVE_NEW_FAMILY,
        object_hashes=hashes,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )
    return ApprovalCommand(
        actor_id=actor_id,
        action=APPROVE_NEW_FAMILY,
        object_hashes=hashes,
        source_command_hash=digest,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )


def test_approve_new_family_required_after_oos_rejection(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    first = db.create_family("first-family")
    second = db.create_family("second-before-reject")
    assert first != second

    family_id, _, rejected = _register_run(db)
    walk(db, rejected, ("DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING", "REJECTED"))
    with pytest.raises(ApprovalError, match="approval required after OOS rejection"):
        db.create_family("blocked")

    with pytest.raises(ApprovalError, match="ambiguous approval"):
        approve_new_family(
            db,
            make_new_family_approval(
                rejected_family_id=family_id,
                proposed_family_id="next-family",
                hashes={"family_id": family_id},
            ),
            allowlist=ALLOWLIST,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="unallowlisted actor"):
        approve_new_family(
            db,
            make_new_family_approval(
                rejected_family_id=family_id,
                proposed_family_id="next-family",
                actor_id="intruder",
            ),
            allowlist=ALLOWLIST,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="rejected family is required"):
        approve_new_family(
            db,
            make_new_family_approval(
                rejected_family_id=second,
                proposed_family_id="next-family",
            ),
            allowlist=ALLOWLIST,
            now=NOW,
        )
    with pytest.raises(ApprovalError, match="proposed family must differ"):
        approve_new_family(
            db,
            make_new_family_approval(
                rejected_family_id=family_id,
                proposed_family_id=family_id,
            ),
            allowlist=ALLOWLIST,
            now=NOW,
        )

    created = approve_new_family(
        db,
        make_new_family_approval(
            rejected_family_id=family_id,
            proposed_family_id="next-family",
        ),
        allowlist=ALLOWLIST,
        now=NOW,
    )
    assert created == "next-family"
    replayed = approve_new_family(
        db,
        make_new_family_approval(
            rejected_family_id=family_id,
            proposed_family_id="next-family",
        ),
        allowlist=ALLOWLIST,
        now=NOW,
    )
    assert replayed == "next-family"
    _, _, successor = _register_run(db, family_id=created)
    walk(db, successor, ("DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING"))
    assert db.get_budget(created).oos_evaluations == 1
    assert states_of(db, successor)[-1] == "OOS_RUNNING"
