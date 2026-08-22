from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from trading_desk.config import canonical_json, sha256_hex
from trading_desk.state import db as state_db
from trading_desk.state.db import Database, RunIdentity
from trading_desk.storage.artifacts import ArtifactStore

COMMIT = "c" * 40


def _digest(label: str) -> str:
    return sha256_hex(label)


def _run_hashes() -> dict[str, str]:
    return {
        "data_snapshot_hash": _digest("snapshot"),
        "derived_data_hash": _digest("derived"),
        "validation_policy_hash": _digest("validation-policy-v2"),
        "execution_policy_hash": _digest("execution"),
    }


def _register_run(db: Database) -> tuple[str, str, RunIdentity]:
    family_id = db.create_family()
    version_id = db.register_version(
        family_id,
        code_commit=COMMIT,
        spec={"lookback": 20, "threshold": 1.5},
    )
    run = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        **_run_hashes(),
    )
    return family_id, version_id, run


def test_canonical_artifact_bytes_are_hashed_atomically_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def tracking_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        src_s, dst_s = os.fspath(src), os.fspath(dst)
        replaced.append((src_s, dst_s))
        assert Path(src_s).exists()
        assert Path(src_s).name.endswith(".tmp")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)

    store = ArtifactStore(tmp_path / "artifacts")
    payload = {"b": 2, "a": 1}
    canonical = canonical_json(payload).encode("utf-8")
    digest = store.put_json(payload)

    assert digest == sha256_hex(canonical)
    assert replaced, "expected atomic rename from a temporary path"
    src, dst = replaced[0]
    assert not Path(src).exists()
    dest = Path(dst)
    assert dest.exists()
    assert dest.read_bytes() == canonical
    assert store.path_for(digest) == dest
    assert list(store.root.rglob("*.tmp")) == []

    raw = b"raw-bytes"
    raw_digest = store.put_bytes(raw)
    assert raw_digest == sha256_hex(raw)
    raw_path = store.path_for(raw_digest)
    assert raw_path.read_bytes() == raw
    stat_before = raw_path.stat()
    assert store.put_bytes(raw) == raw_digest
    stat_after = raw_path.stat()
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns
    assert raw_path.read_bytes() == raw


def test_strategy_version_is_immutable_and_rerun_gets_new_run_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id = db.create_family()
    spec = {"lookback": 20, "threshold": 1.5}
    version_id = db.register_version(family_id, code_commit=COMMIT, spec=spec)
    assert (
        db.register_version(
            family_id,
            code_commit=COMMIT,
            spec=spec,
            strategy_version_id=version_id,
        )
        == version_id
    )
    with pytest.raises(ValueError, match="immutable"):
        db.register_version(
            family_id,
            code_commit=COMMIT,
            spec={"lookback": 21, "threshold": 1.5},
            strategy_version_id=version_id,
        )

    hashes = _run_hashes()
    first = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        **hashes,
    )
    second = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        **hashes,
    )
    assert first.run_id != second.run_id
    assert first.strategy_version_id == second.strategy_version_id == version_id
    assert first.family_id == family_id
    assert db.get_run(first.run_id) == first


def test_sqlite_wal_foreign_keys_unique_idempotency_and_append_only_transitions(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert {
            "family_id",
            "strategy_version_id",
            "code_commit",
            "data_snapshot_hash",
            "derived_data_hash",
            "validation_policy_hash",
            "execution_policy_hash",
            "run_id",
        } <= columns
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO strategy_versions (
                       strategy_version_id, family_id, code_commit, spec_json, spec_hash, created_at
                   ) VALUES ('v1', 'missing-family', 'c', '{}', ?, '2026-08-23T00:00:00+00:00')""",
                (_digest("spec"),),
            )
    finally:
        conn.close()

    family_id, version_id, run = _register_run(db)
    with db.transaction() as txn:
        db.append_transition(
            txn,
            run_id=run.run_id,
            from_state=None,
            to_state="DRAFT",
            idempotency_key="transition-1",
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as txn:
            db.append_transition(
                txn,
                run_id=run.run_id,
                from_state="DRAFT",
                to_state="DEVELOPMENT_RUNNING",
                idempotency_key="transition-1",
            )

    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE transitions SET to_state = 'TAMPERED'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM transitions")
    finally:
        conn.close()

    db.record_approval(
        actor_id="user-1",
        action="start_paper",
        object_hashes={"strategy_version_id": version_id, "run_id": run.run_id},
        source_command_hash=_digest("cmd"),
        idempotency_key="approve-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.record_approval(
            actor_id="user-1",
            action="start_paper",
            object_hashes={"strategy_version_id": version_id, "run_id": run.run_id},
            source_command_hash=_digest("cmd"),
            idempotency_key="approve-1",
        )
    db.enqueue_outbox(
        topic="alert",
        payload={"event": "READY_FOR_PAPER"},
        idempotency_key="outbox-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.enqueue_outbox(
            topic="alert",
            payload={"event": "READY_FOR_PAPER"},
            idempotency_key="outbox-1",
        )
    db.upsert_paper_state(family_id=family_id, status="READY_FOR_PAPER")
    assert db.get_paper_state(family_id)["status"] == "READY_FOR_PAPER"


def test_state_transition_and_budget_commit_and_rollback_together(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, _version_id, run = _register_run(db)
    assert db.get_budget(family_id).performance_evaluated_versions == 0

    db.commit_transition_and_budget(
        run_id=run.run_id,
        family_id=family_id,
        from_state="DRAFT",
        to_state="DEVELOPMENT_RUNNING",
        idempotency_key="budget-commit",
        budget_kind="performance",
    )
    assert db.get_budget(family_id).performance_evaluated_versions == 1
    assert [row["to_state"] for row in db.list_transitions(run.run_id)] == ["DEVELOPMENT_RUNNING"]

    with pytest.raises(RuntimeError, match="forced rollback"):
        with db.transaction() as txn:
            db.append_transition(
                txn,
                run_id=run.run_id,
                from_state="DEVELOPMENT_RUNNING",
                to_state="OOS_RUNNING",
                idempotency_key="budget-rollback",
            )
            db.consume_budget(txn, family_id=family_id, kind="oos")
            raise RuntimeError("forced rollback")

    budget = db.get_budget(family_id)
    assert budget.performance_evaluated_versions == 1
    assert budget.oos_evaluations == 0
    assert [row["to_state"] for row in db.list_transitions(run.run_id)] == ["DEVELOPMENT_RUNNING"]


def test_mismatched_family_id_fails_closed_and_rolls_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id, version_id, run = _register_run(db)
    other_family = db.create_family()

    with pytest.raises(ValueError, match="family_id does not match strategy version"):
        db.create_run(
            family_id=other_family,
            strategy_version_id=version_id,
            code_commit=COMMIT,
            **_run_hashes(),
        )

    hashes = _run_hashes()
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO runs (
                       run_id, family_id, strategy_version_id, code_commit,
                       data_snapshot_hash, derived_data_hash,
                       validation_policy_hash, execution_policy_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "orphan-run",
                    other_family,
                    version_id,
                    COMMIT,
                    hashes["data_snapshot_hash"],
                    hashes["derived_data_hash"],
                    hashes["validation_policy_hash"],
                    hashes["execution_policy_hash"],
                    "2026-08-23T00:00:00+00:00",
                ),
            )
    finally:
        conn.close()

    with pytest.raises(ValueError, match="family_id does not match run"):
        db.commit_transition_and_budget(
            run_id=run.run_id,
            family_id=other_family,
            from_state="DRAFT",
            to_state="DEVELOPMENT_RUNNING",
            idempotency_key="mismatch-family",
            budget_kind="performance",
        )

    assert db.get_budget(family_id).performance_evaluated_versions == 0
    assert db.get_budget(other_family).performance_evaluated_versions == 0
    assert db.list_transitions(run.run_id) == []
    with pytest.raises(ValueError, match="unknown run_id"):
        db.get_run("orphan-run")


def test_register_version_integrity_error_uses_immutability_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id = db.create_family()
    spec = {"lookback": 20, "threshold": 1.5}
    version_id = db.register_version(
        family_id,
        code_commit=COMMIT,
        spec=spec,
        strategy_version_id="version-race-1",
    )
    real_get = state_db._get_version_row
    miss_next = True

    def racing_get(conn: sqlite3.Connection, strategy_version_id: str) -> sqlite3.Row | None:
        nonlocal miss_next
        if miss_next:
            miss_next = False
            return None
        return real_get(conn, strategy_version_id)

    monkeypatch.setattr(state_db, "_get_version_row", racing_get)
    assert (
        db.register_version(
            family_id,
            code_commit=COMMIT,
            spec=spec,
            strategy_version_id=version_id,
        )
        == version_id
    )
    miss_next = True
    with pytest.raises(ValueError, match="immutable"):
        db.register_version(
            family_id,
            code_commit=COMMIT,
            spec={"lookback": 21, "threshold": 1.5},
            strategy_version_id=version_id,
        )
