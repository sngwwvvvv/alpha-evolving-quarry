from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from trading_desk.config import canonical_json, sha256_hex, utc_now

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
BudgetKind = Literal["performance", "oos"]


@dataclass(frozen=True, slots=True)
class RunIdentity:
    family_id: str
    strategy_version_id: str
    code_commit: str
    data_snapshot_hash: str
    derived_data_hash: str
    validation_policy_hash: str
    execution_policy_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class Budget:
    family_id: str
    performance_evaluated_versions: int
    oos_evaluations: int
    max_performance_evaluated_versions: int
    max_oos_evaluations: int


def _utc_timestamp() -> str:
    return utc_now().isoformat()


def _get_version_row(conn: sqlite3.Connection, strategy_version_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT family_id, code_commit, spec_hash
           FROM strategy_versions
           WHERE strategy_version_id = ?""",
        (strategy_version_id,),
    ).fetchone()


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _require_hash(name: str, value: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _require_immutable_version(
    existing: sqlite3.Row,
    *,
    family_id: str,
    code_commit: str,
    spec_hash: str,
) -> None:
    if (
        existing["spec_hash"] != spec_hash
        or existing["family_id"] != family_id
        or existing["code_commit"] != code_commit
    ):
        raise ValueError("strategy version is immutable")


def family_ids_with_oos_rejection(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute(
        """SELECT DISTINCT runs.family_id
           FROM transitions
           JOIN runs ON runs.run_id = transitions.run_id
           WHERE transitions.to_state = 'REJECTED'"""
    ).fetchall()
    return frozenset(str(row["family_id"]) for row in rows)


def insert_family_row(conn: sqlite3.Connection, family_id: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO families (family_id, created_at) VALUES (?, ?)",
        (family_id, created_at),
    )
    conn.execute(
        """INSERT INTO budgets (
               family_id,
               performance_evaluated_versions,
               oos_evaluations,
               max_performance_evaluated_versions,
               max_oos_evaluations
           ) VALUES (?, 0, 0, 8, 1)""",
        (family_id,),
    )


def _row_to_run(row: sqlite3.Row) -> RunIdentity:
    return RunIdentity(
        family_id=row["family_id"],
        strategy_version_id=row["strategy_version_id"],
        code_commit=row["code_commit"],
        data_snapshot_hash=row["data_snapshot_hash"],
        derived_data_hash=row["derived_data_hash"],
        validation_policy_hash=row["validation_policy_hash"],
        execution_policy_hash=row["execution_policy_hash"],
        run_id=row["run_id"],
    )


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def create_family(self, family_id: str | None = None) -> str:
        family_id = _require_text("family_id", family_id or uuid.uuid4().hex)
        now = _utc_timestamp()
        with self.transaction() as conn:
            if family_ids_with_oos_rejection(conn):
                from trading_desk.state.approvals import ApprovalError

                raise ApprovalError("approval required after OOS rejection")
            insert_family_row(conn, family_id, now)
        return family_id

    def register_version(
        self,
        family_id: str,
        *,
        code_commit: str,
        spec: Mapping[str, Any],
        strategy_version_id: str | None = None,
    ) -> str:
        family_id = _require_text("family_id", family_id)
        code_commit = _require_text("code_commit", code_commit)
        spec_json = canonical_json(spec)
        spec_hash = sha256_hex(spec_json)
        version_id = strategy_version_id or sha256_hex(
            canonical_json({"code_commit": code_commit, "family_id": family_id, "spec": spec})
        )
        version_id = _require_text("strategy_version_id", version_id)
        with self.transaction() as conn:
            existing = _get_version_row(conn, version_id)
            if existing is not None:
                _require_immutable_version(
                    existing,
                    family_id=family_id,
                    code_commit=code_commit,
                    spec_hash=spec_hash,
                )
                return version_id
            try:
                conn.execute(
                    """INSERT INTO strategy_versions (
                           strategy_version_id, family_id, code_commit, spec_json, spec_hash, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (version_id, family_id, code_commit, spec_json, spec_hash, _utc_timestamp()),
                )
            except sqlite3.IntegrityError:
                existing = _get_version_row(conn, version_id)
                if existing is None:
                    raise
                _require_immutable_version(
                    existing,
                    family_id=family_id,
                    code_commit=code_commit,
                    spec_hash=spec_hash,
                )
        return version_id

    def create_run(
        self,
        *,
        family_id: str,
        strategy_version_id: str,
        code_commit: str,
        data_snapshot_hash: str,
        derived_data_hash: str,
        validation_policy_hash: str,
        execution_policy_hash: str,
    ) -> RunIdentity:
        identity = RunIdentity(
            family_id=_require_text("family_id", family_id),
            strategy_version_id=_require_text("strategy_version_id", strategy_version_id),
            code_commit=_require_text("code_commit", code_commit),
            data_snapshot_hash=_require_hash("data_snapshot_hash", data_snapshot_hash),
            derived_data_hash=_require_hash("derived_data_hash", derived_data_hash),
            validation_policy_hash=_require_hash("validation_policy_hash", validation_policy_hash),
            execution_policy_hash=_require_hash("execution_policy_hash", execution_policy_hash),
            run_id=uuid.uuid4().hex,
        )
        with self.transaction() as conn:
            version = conn.execute(
                "SELECT family_id, code_commit FROM strategy_versions WHERE strategy_version_id = ?",
                (identity.strategy_version_id,),
            ).fetchone()
            if version is None:
                raise ValueError("unknown strategy_version_id")
            if (
                version["family_id"] != identity.family_id
                or version["code_commit"] != identity.code_commit
            ):
                raise ValueError("family_id does not match strategy version")
            conn.execute(
                """INSERT INTO runs (
                       run_id, family_id, strategy_version_id, code_commit,
                       data_snapshot_hash, derived_data_hash,
                       validation_policy_hash, execution_policy_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identity.run_id,
                    identity.family_id,
                    identity.strategy_version_id,
                    identity.code_commit,
                    identity.data_snapshot_hash,
                    identity.derived_data_hash,
                    identity.validation_policy_hash,
                    identity.execution_policy_hash,
                    _utc_timestamp(),
                ),
            )
        return identity

    def get_run(self, run_id: str) -> RunIdentity:
        run_id = _require_text("run_id", run_id)
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("unknown run_id")
            return _row_to_run(row)
        finally:
            conn.close()

    def append_transition(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        from_state: str | None,
        to_state: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> int:
        run_id = _require_text("run_id", run_id)
        to_state = _require_text("to_state", to_state)
        idempotency_key = _require_text("idempotency_key", idempotency_key)
        if from_state is not None:
            from_state = _require_text("from_state", from_state)
        cursor = conn.execute(
            """INSERT INTO transitions (
                   run_id, from_state, to_state, reason, idempotency_key, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, from_state, to_state, reason, idempotency_key, _utc_timestamp()),
        )
        return int(cursor.lastrowid or 0)

    def consume_budget(
        self,
        conn: sqlite3.Connection,
        *,
        family_id: str,
        kind: BudgetKind,
    ) -> None:
        family_id = _require_text("family_id", family_id)
        if kind == "performance":
            column = "performance_evaluated_versions"
        elif kind == "oos":
            column = "oos_evaluations"
        else:
            raise ValueError(f"unknown budget kind: {kind}")
        cursor = conn.execute(
            f"UPDATE budgets SET {column} = {column} + 1 WHERE family_id = ?",
            (family_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("unknown family budget")

    def commit_transition_and_budget(
        self,
        *,
        run_id: str,
        family_id: str | None = None,
        from_state: str | None,
        to_state: str,
        idempotency_key: str,
        budget_kind: BudgetKind,
        reason: str | None = None,
    ) -> None:
        run_id = _require_text("run_id", run_id)
        if family_id is not None:
            family_id = _require_text("family_id", family_id)
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT family_id FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("unknown run_id")
            run_family_id = run["family_id"]
            if family_id is not None and family_id != run_family_id:
                raise ValueError("family_id does not match run")
            self.append_transition(
                conn,
                run_id=run_id,
                from_state=from_state,
                to_state=to_state,
                idempotency_key=idempotency_key,
                reason=reason,
            )
            self.consume_budget(conn, family_id=run_family_id, kind=budget_kind)

    def list_transitions(self, run_id: str) -> list[sqlite3.Row]:
        run_id = _require_text("run_id", run_id)
        conn = self.connect()
        try:
            return list(
                conn.execute(
                    "SELECT * FROM transitions WHERE run_id = ? ORDER BY transition_id",
                    (run_id,),
                )
            )
        finally:
            conn.close()

    def get_budget(self, family_id: str) -> Budget:
        family_id = _require_text("family_id", family_id)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM budgets WHERE family_id = ?",
                (family_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown family")
            return Budget(
                family_id=row["family_id"],
                performance_evaluated_versions=row["performance_evaluated_versions"],
                oos_evaluations=row["oos_evaluations"],
                max_performance_evaluated_versions=row["max_performance_evaluated_versions"],
                max_oos_evaluations=row["max_oos_evaluations"],
            )
        finally:
            conn.close()

    def record_approval(
        self,
        *,
        actor_id: str,
        action: str,
        object_hashes: Mapping[str, Any],
        source_command_hash: str,
        idempotency_key: str,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        actor_id = _require_text("actor_id", actor_id)
        action = _require_text("action", action)
        source_command_hash = _require_hash("source_command_hash", source_command_hash)
        idempotency_key = _require_text("idempotency_key", idempotency_key)
        approval_id = uuid.uuid4().hex

        def _insert(txn: sqlite3.Connection) -> str:
            txn.execute(
                """INSERT INTO approvals (
                       approval_id, actor_id, action, object_hashes_json,
                       source_command_hash, idempotency_key, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id,
                    actor_id,
                    action,
                    canonical_json(object_hashes),
                    source_command_hash,
                    idempotency_key,
                    _utc_timestamp(),
                ),
            )
            return approval_id

        if conn is not None:
            return _insert(conn)
        with self.transaction() as txn:
            return _insert(txn)

    def get_approval(
        self,
        idempotency_key: str,
        conn: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        idempotency_key = _require_text("idempotency_key", idempotency_key)

        def _fetch(txn: sqlite3.Connection) -> sqlite3.Row | None:
            return txn.execute(
                "SELECT * FROM approvals WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()

        if conn is not None:
            return _fetch(conn)
        opened = self.connect()
        try:
            return _fetch(opened)
        finally:
            opened.close()

    def upsert_paper_state(
        self,
        *,
        family_id: str,
        status: str,
        payload: Mapping[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        family_id = _require_text("family_id", family_id)
        status = _require_text("status", status)
        payload_json = None if payload is None else canonical_json(payload)

        def _upsert(txn: sqlite3.Connection) -> None:
            txn.execute(
                """INSERT INTO paper_state (family_id, status, payload_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(family_id) DO UPDATE SET
                     status = excluded.status,
                     payload_json = excluded.payload_json,
                     updated_at = excluded.updated_at""",
                (family_id, status, payload_json, _utc_timestamp()),
            )

        if conn is not None:
            _upsert(conn)
            return
        with self.transaction() as txn:
            _upsert(txn)

    def get_paper_state(self, family_id: str) -> dict[str, Any]:
        family_id = _require_text("family_id", family_id)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM paper_state WHERE family_id = ?",
                (family_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown paper state")
            return {
                "family_id": row["family_id"],
                "status": row["status"],
                "payload_json": row["payload_json"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def enqueue_outbox(
        self,
        *,
        topic: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        next_attempt_at: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        topic = _require_text("topic", topic)
        idempotency_key = _require_text("idempotency_key", idempotency_key)

        def _insert(txn: sqlite3.Connection) -> int:
            cursor = txn.execute(
                """INSERT INTO outbox (
                       topic, payload_json, idempotency_key, status,
                       attempt_count, next_attempt_at, published_revision_id, created_at
                   ) VALUES (?, ?, ?, 'PUBLISH_PENDING', 0, ?, NULL, ?)""",
                (
                    topic,
                    canonical_json(payload),
                    idempotency_key,
                    next_attempt_at,
                    created_at or _utc_timestamp(),
                ),
            )
            return int(cursor.lastrowid or 0)

        if conn is not None:
            return _insert(conn)
        with self.transaction() as txn:
            return _insert(txn)
