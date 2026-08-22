from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading_desk.config import UTC, sha256_hex
from trading_desk.ledger.bundle import LedgerBundle
from trading_desk.publish.publisher import (
    FakeWikiSink,
    PublicationRevision,
    WikiPublishError,
    process_publish_outbox,
    publish_revision,
)
from trading_desk.state.db import Database
from trading_desk.state.outbox import BUZZ_WARNING_AFTER
from trading_desk.storage.artifacts import ArtifactStore

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
COMMIT = "c" * 40


def _ledger(*, run_id: str = "run-1", summary: str = "development FAIL", kind: str = "development") -> LedgerBundle:
    return LedgerBundle(
        kind=kind,
        outcome="FAIL",
        executive_summary=summary,
        gates_failed=("cagr",),
        gates_achieved=("max_drawdown_aggregate",),
        loss_attribution={
            "by_cost_type": {"fees": "-0.04", "funding": "0", "slippage": "-0.01"},
            "by_direction": {"LONG": "-1.50"},
            "by_exit_reason": {"stop_loss": "-1.50"},
            "by_period": {"0": "-1.50"},
            "by_regime": {"BULL": "-1.50"},
            "by_symbol": {"BTCUSDT": "-1.50"},
        },
        run_id=run_id,
        version_id="ver-1",
        trade_references=(sha256_hex("trade-1"),),
        result_bundle_hash="b" * 64,
    )


def _harness(tmp_path: Path) -> tuple[Database, ArtifactStore]:
    return Database(tmp_path / "state.sqlite3"), ArtifactStore(tmp_path / "artifacts")


def outbox_rows(db: Database) -> list[dict[str, object]]:
    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM outbox ORDER BY outbox_id"))
    finally:
        conn.close()
    return [
        {
            "attempt_count": int(row["attempt_count"]),
            "idempotency_key": row["idempotency_key"],
            "next_attempt_at": row["next_attempt_at"],
            "payload": json.loads(row["payload_json"]),
            "published_revision_id": row["published_revision_id"],
            "status": row["status"],
            "topic": row["topic"],
        }
        for row in rows
    ]


def wiki_rows(db: Database) -> list[dict[str, object]]:
    return [row for row in outbox_rows(db) if row["topic"] == "wiki_publish"]


class FailNSink:
    def __init__(self, failures: int, inner: FakeWikiSink | None = None) -> None:
        self.failures = failures
        self.calls = 0
        self.inner = inner or FakeWikiSink()

    def publish(self, revision: PublicationRevision) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise WikiPublishError("wiki unavailable")
        return self.inner.publish(revision)


def test_publish_is_idempotent_by_revision_id(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FakeWikiSink()
    ledger = _ledger()
    first = publish_revision(db, store, ledger, sink=sink, now=NOW)
    second = publish_revision(db, store, ledger, sink=sink, now=NOW + timedelta(minutes=1))
    assert first.revision_id == second.revision_id
    assert len(sink.order) == 1
    assert sink.pages[first.revision_id].bundle_hash == first.bundle_hash
    rows = wiki_rows(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "PUBLISHED"
    assert rows[0]["published_revision_id"] == first.revision_id


def test_ledger_revisions_are_append_only(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FakeWikiSink()
    first = publish_revision(db, store, _ledger(run_id="run-1", summary="first"), sink=sink, now=NOW)
    second = publish_revision(
        db,
        store,
        _ledger(run_id="run-2", summary="second"),
        sink=sink,
        now=NOW,
        previous_revision_id=first.revision_id,
    )
    assert first.revision_id != second.revision_id
    assert sink.order == [first.revision_id, second.revision_id]
    assert sink.pages[first.revision_id].markdown == first.markdown
    assert "first" in sink.pages[first.revision_id].markdown
    assert "second" in sink.pages[second.revision_id].markdown
    with pytest.raises(WikiPublishError, match="overwrite"):
        sink.overwrite(first.revision_id, "# tampered")


def test_wiki_sink_is_isolated_from_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, store = _harness(tmp_path)
    sink = FakeWikiSink()
    revision = publish_revision(db, store, _ledger(), sink=sink, now=NOW)
    sqlite_path = tmp_path / "state.sqlite3"
    assert sqlite_path.exists()

    def boom(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise AssertionError("wiki must not open sqlite")

    monkeypatch.setattr(sqlite3, "connect", boom)
    assert sink.publish(revision) == revision.revision_id
    assert sink.get(revision.revision_id) is not None
    page = sink.get(revision.revision_id)
    assert page is not None
    assert "sqlite" not in page.markdown.lower()
    assert str(sqlite_path) not in page.markdown


def test_wiki_failure_is_non_blocking(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FailNSink(failures=99)
    ledger = _ledger()
    revision = publish_revision(db, store, ledger, sink=sink, now=NOW)
    assert isinstance(revision, PublicationRevision)
    assert revision.revision_id
    rows = wiki_rows(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "PUBLISH_PENDING"
    assert rows[0]["published_revision_id"] is None
    assert rows[0]["attempt_count"] == 1
    family_id = db.create_family()
    version_id = db.register_version(family_id, code_commit=COMMIT, spec={"lookback": 20})
    run = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        data_snapshot_hash="a" * 64,
        derived_data_hash="b" * 64,
        validation_policy_hash="c" * 64,
        execution_policy_hash="d" * 64,
    )
    assert run.run_id


def test_markdown_json_bundle_generation(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FakeWikiSink()
    ledger = _ledger()
    revision = publish_revision(db, store, ledger, sink=sink, now=NOW, namespace="backtest")
    assert "executive summary" in revision.markdown.lower()
    assert ledger.executive_summary in revision.markdown
    assert "cagr" in revision.markdown
    assert ledger.run_id in revision.markdown
    assert ledger.result_bundle_hash in revision.markdown
    payload = revision.json_payload
    assert payload["run_id"] == ledger.run_id
    assert payload["gates_failed"] == ["cagr"]
    assert payload["kind"] == "development"
    assert store.path_for(store.put_json(revision.to_payload())).exists()
    assert revision.namespace == "backtest"
    assert len(revision.revision_id) == 64


def test_paper_namespace_is_separate_from_backtest(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FakeWikiSink()
    ledger = _ledger(kind="development")
    paper_ledger = _ledger(kind="paper", summary="paper daily pause", run_id="paper-run")
    backtest = publish_revision(db, store, ledger, sink=sink, namespace="backtest", now=NOW)
    paper = publish_revision(db, store, paper_ledger, sink=sink, namespace="paper", now=NOW)
    assert backtest.namespace == "backtest"
    assert paper.namespace == "paper"
    assert backtest.revision_id != paper.revision_id
    assert set(sink.order) == {backtest.revision_id, paper.revision_id}


def test_retry_schedule_immediate_5m_15m_60m_6h_and_24h_warning(tmp_path: Path) -> None:
    db, store = _harness(tmp_path)
    sink = FailNSink(failures=99)
    revision = publish_revision(db, store, _ledger(), sink=sink, now=NOW)
    row = wiki_rows(db)[0]
    assert row["status"] == "PUBLISH_PENDING"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert process_publish_outbox(db, sink, now=NOW) == []

    t2 = NOW + timedelta(minutes=5)
    assert process_publish_outbox(db, sink, now=t2) == []
    assert wiki_rows(db)[0]["attempt_count"] == 2
    assert wiki_rows(db)[0]["next_attempt_at"] == (t2 + timedelta(minutes=15)).isoformat()

    t3 = t2 + timedelta(minutes=15)
    assert process_publish_outbox(db, sink, now=t3) == []
    assert wiki_rows(db)[0]["attempt_count"] == 3
    assert wiki_rows(db)[0]["next_attempt_at"] == (t3 + timedelta(minutes=60)).isoformat()

    t4 = t3 + timedelta(minutes=60)
    assert process_publish_outbox(db, sink, now=t4) == []
    assert wiki_rows(db)[0]["attempt_count"] == 4
    assert wiki_rows(db)[0]["next_attempt_at"] == (t4 + timedelta(hours=6)).isoformat()
    assert process_publish_outbox(db, sink, now=t4 + timedelta(minutes=5)) == []

    t5 = t4 + timedelta(hours=6)
    assert process_publish_outbox(db, sink, now=t5) == []
    assert wiki_rows(db)[0]["attempt_count"] == 5
    assert wiki_rows(db)[0]["next_attempt_at"] == (t5 + timedelta(hours=6)).isoformat()

    warning_time = NOW + BUZZ_WARNING_AFTER
    assert process_publish_outbox(db, sink, now=warning_time) == []
    warnings = [row for row in outbox_rows(db) if row["topic"] == "buzz_warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["event"] == "OUTBOX_RETRY_EXCEEDED_24H"
    assert process_publish_outbox(db, sink, now=warning_time) == []
    assert len([row for row in outbox_rows(db) if row["topic"] == "buzz_warning"]) == 1

    sink.failures = sink.calls
    published = process_publish_outbox(db, sink, now=warning_time + timedelta(hours=6))
    assert published == [revision.revision_id]
    done = wiki_rows(db)[0]
    assert done["status"] == "PUBLISHED"
    assert done["published_revision_id"] == revision.revision_id


def test_publisher_has_no_network_imports() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "trading_desk" / "publish"
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "websockets",
        "http.client",
        "binance",
    )
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
        ), path.name
    sink_src = (root / "publisher.py").read_text(encoding="utf-8")
    tree = ast.parse(sink_src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "FakeWikiSink":
            class_src = ast.get_source_segment(sink_src, node) or ""
            assert "sqlite3" not in class_src
            assert "Database" not in class_src
