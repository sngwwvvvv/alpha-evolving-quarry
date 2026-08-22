from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_desk.config import SUPPORTED_SYMBOLS, UTC, canonical_json, sha256_hex
from trading_desk.data.contracts import (
    DAILY_TRANSFORMATION,
    DAY,
    EMA_200_WARMUP_DAYS,
    MINUTE,
    OK,
    TIMEFRAME_1D,
    ContractMetadata,
    DataSnapshot,
    DerivedBar,
    Funding,
    Kline1m,
    source_hash,
    transformation_hash,
)
from trading_desk.ledger.bundle import LedgerBundle, MutationManifest, build_ledger_bundle
from trading_desk.state.db import Database
from trading_desk.state.transitions import TransitionError
from trading_desk.storage.artifacts import ArtifactStore
from trading_desk.strategy.models import ExecutionPolicy, StrategyParameters
from trading_desk.validation import (
    EquityPoint,
    EvaluationArtifacts,
    EvaluationPolicy,
    GateResult,
    ResultBundle,
    SealedOos,
    TradeRecord,
    evaluate_development,
    research_inputs,
)
from trading_desk.validation.gates import evaluate_gates
from trading_desk.workflows.research_loop import (
    SuccessorMutationError,
    coding_inputs,
    propose_successor_mutation,
    run_development_cycle,
    run_sealed_oos,
)

COMMIT = "c" * 40
WORKTREE = {
    "disposable": True,
    "path": "/tmp/worktrees/strategy-v1",
    "version_id": "ver-1",
}
EVAL_START = datetime(2020, 7, 19, tzinfo=UTC)
DAILY_START = EVAL_START - timedelta(days=EMA_200_WARMUP_DAYS)
STARTING_EQUITY = Decimal("10000")
POLICY = ExecutionPolicy()
DEV_START = datetime(2020, 1, 1, tzinfo=UTC)
DEV_END = datetime(2021, 1, 1, tzinfo=UTC)
OOS_END = datetime(2021, 7, 1, tzinfo=UTC)
DEV_GATE_NAMES = {
    "cagr",
    "calmar",
    "max_drawdown_aggregate",
    "max_drawdown_windows",
    "positive_window_fraction",
    "trades_aggregate",
    "trades_per_symbol",
    "profit_factor_base",
    "profit_factor_stress",
    "profit_factor_bootstrap",
    "psr",
    "dsr",
    "concentration_symbol",
    "concentration_period",
    "neighborhood",
    "leave_one_symbol_out",
    "mutations_per_successor",
}


def _kline(symbol: str, open_time: datetime, price: Decimal) -> Kline1m:
    return Kline1m(
        symbol=symbol,
        open_time=open_time,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1"),
    )


def _fixed_snapshot(*, hours: int = 24) -> DataSnapshot:
    klines: list[Kline1m] = []
    for symbol in SUPPORTED_SYMBOLS:
        for index in range(hours):
            price = Decimal(100 + index)
            hour = EVAL_START + timedelta(hours=index)
            for minute in range(60):
                klines.append(_kline(symbol, hour + timedelta(minutes=minute), price))
    funding: list[Funding] = []
    first = min(bar.open_time for bar in klines)
    end = max(bar.open_time for bar in klines) + MINUTE
    for symbol in SUPPORTED_SYMBOLS:
        slot = first.replace(hour=(first.hour // 8) * 8, minute=0, second=0, microsecond=0)
        while slot < end:
            funding.append(Funding(symbol=symbol, funding_time=slot, funding_rate=Decimal("0.0001")))
            slot += timedelta(hours=8)
    metadata = tuple(
        ContractMetadata(
            symbol=symbol,
            effective_from=EVAL_START,
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            listing_state="TRADING",
        )
        for symbol in SUPPORTED_SYMBOLS
    )
    source = source_hash(klines, funding, metadata)
    tf_hash = transformation_hash(source, DAILY_TRANSFORMATION)
    daily: list[DerivedBar] = []
    for symbol in SUPPORTED_SYMBOLS:
        for index in range(EMA_200_WARMUP_DAYS):
            close = Decimal(100 + index)
            daily.append(
                DerivedBar(
                    symbol=symbol,
                    timeframe=TIMEFRAME_1D,
                    open_time=DAILY_START + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=Decimal("1"),
                    source_hash=source,
                    transformation_version=DAILY_TRANSFORMATION,
                    transformation_hash=tf_hash,
                )
            )
    return DataSnapshot(
        klines_1m=tuple(klines),
        funding=tuple(funding),
        metadata=metadata,
        daily_bars=tuple(daily),
        source_hash=source,
        evaluation_start=EVAL_START,
    )


def _trade_record(
    symbol: str,
    day: int,
    net_pnl: str,
    *,
    month: int = 1,
    fees: str = "0.04",
    funding: str = "0",
    slippage_cost: str = "0.01",
    direction: str = "LONG",
    exit_reason: str = "take_profit",
    regime: str = "BULL",
) -> TradeRecord:
    entry = datetime(2020, month, min(day, 28), 12, tzinfo=UTC)
    exit_time = datetime(2020, month, min(day, 28), 18, tzinfo=UTC)
    return TradeRecord(
        symbol=symbol,
        direction=direction,
        entry_time=entry,
        exit_time=exit_time,
        net_pnl=Decimal(net_pnl),
        fees=Decimal(fees),
        funding=Decimal(funding),
        slippage_cost=Decimal(slippage_cost),
        exit_reason=exit_reason,
        regime=regime,
    )


def _passing_dev_metrics() -> dict[str, object]:
    return {
        "cagr": Decimal("0.12"),
        "calmar": Decimal("1.00"),
        "max_drawdown": Decimal("0.10"),
        "window_max_drawdowns": (Decimal("0.08"), Decimal("0.09")),
        "positive_window_fraction": Decimal("0.70"),
        "trade_count": 200,
        "trade_count_by_symbol": {symbol: 50 for symbol in SUPPORTED_SYMBOLS},
        "profit_factor": Decimal("1.30"),
        "profit_factor_stress": Decimal("1.15"),
        "profit_factor_bootstrap_lower_bound": Decimal("1.05"),
        "psr": Decimal("0.95"),
        "dsr": Decimal("0.99"),
        "concentration_symbol_max": Decimal("0.40"),
        "concentration_period_max": Decimal("0.40"),
        "neighborhood_survival_fraction": Decimal("0.70"),
        "leave_one_out_surviving_count": 4,
        "mutation_count": 0,
        "has_prior_version": False,
        "window_count": 2,
        "total_return": Decimal("0.12"),
    }


def _pass_development(artifacts: EvaluationArtifacts, policy: EvaluationPolicy | None = None) -> ResultBundle:
    policy = policy or EvaluationPolicy.v2()
    metrics = dict(_passing_dev_metrics())
    metrics["has_prior_version"] = artifacts.prior_version_id is not None
    metrics["mutation_count"] = artifacts.mutation_count
    gates = evaluate_gates(metrics, policy, kind="development")
    outcome = (
        GateResult.PASS if all(item is GateResult.PASS for item in gates.values()) else GateResult.FAIL
    )
    return ResultBundle(
        kind="development",
        outcome=outcome,
        gates=gates,
        metrics=metrics,
        policy_hash=policy.policy_hash,
        policy_version=policy.version,
    )


def _oos_artifacts(*, profitable: bool) -> EvaluationArtifacts:
    pnl = "5" if profitable else "0"
    end_eq = Decimal("10120") if profitable else Decimal("10000")
    trades = tuple(
        _trade_record(symbol, 3, pnl, month=1)
        for symbol in SUPPORTED_SYMBOLS
        for _ in range(6)
    )
    return EvaluationArtifacts(
        trades=trades,
        starting_equity=STARTING_EQUITY,
        start=DEV_END,
        end=OOS_END,
        equity_curve=(
            EquityPoint(DEV_END, STARTING_EQUITY),
            EquityPoint(OOS_END, end_eq),
        ),
        performance_evaluated_versions=1,
    )


def _manifest(prior_version_id: str) -> MutationManifest:
    return MutationManifest(
        prior_version_id=prior_version_id,
        hypothesis="Widen hourly EMA lookback to reduce false breaks",
        files_changed=("src/trading_desk/strategy/default.py",),
        configuration_fields_changed=("hourly_ema_lookback",),
        expected_causal_effect="Fewer entries, higher profit factor",
        invariant_diff_result="unchanged",
    )


def _harness(tmp_path: Path) -> tuple[Database, ArtifactStore, DataSnapshot]:
    return (
        Database(tmp_path / "state.sqlite3"),
        ArtifactStore(tmp_path / "artifacts"),
        _fixed_snapshot(),
    )


def test_fixed_dataset_flows_through_snapshot_backtest_gates_persistence_and_hashes(
    tmp_path: Path,
) -> None:
    db, store, snapshot = _harness(tmp_path)
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.backtest is not None
    assert cycle.backtest.status == OK
    assert cycle.backtest.trades
    assert cycle.result is not None
    assert cycle.result.kind == "development"
    assert set(cycle.result.gates) == DEV_GATE_NAMES
    assert cycle.ledger is not None
    assert cycle.artifacts is not None
    assert all(row.slippage_cost > 0 for row in cycle.artifacts.trades)
    result_digest = cycle.artifact_digests["result_bundle"]
    ledger_digest = cycle.artifact_digests["ledger"]
    trades_digest = cycle.artifact_digests["trades"]
    assert result_digest == cycle.result.bundle_hash
    assert store.path_for(result_digest).exists()
    assert store.path_for(ledger_digest).exists()
    assert store.path_for(trades_digest).exists()
    replay = evaluate_development(cycle.artifacts)
    assert replay.bundle_hash == cycle.result.bundle_hash
    other = Database(tmp_path / "replay.sqlite3")
    replay_store = ArtifactStore(tmp_path / "replay-artifacts")
    second = run_development_cycle(
        other,
        replay_store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert second.result is not None
    assert second.result.bundle_hash == cycle.result.bundle_hash
    assert second.ledger is not None
    assert second.ledger.gates_failed == cycle.ledger.gates_failed
    assert second.ledger.loss_attribution == cycle.ledger.loss_attribution


def test_development_failure_records_analysis_and_allows_exactly_one_successor_mutation(
    tmp_path: Path,
) -> None:
    db, store, snapshot = _harness(tmp_path)
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.result is not None
    assert cycle.result.outcome is GateResult.FAIL
    assert cycle.analysis_requested is True
    assert cycle.successor_mutation_allowed is True
    assert cycle.state == "ANALYSIS_READY"
    assert cycle.ledger is not None
    assert cycle.ledger.mutation_hypothesis
    assert cycle.oos is None
    states = [row["to_state"] for row in db.list_transitions(cycle.run.run_id)]
    assert states == ["DRAFT", "DEVELOPMENT_RUNNING", "ANALYSIS_READY"]
    assert db.get_budget(cycle.family_id).performance_evaluated_versions == 1
    assert db.get_budget(cycle.family_id).oos_evaluations == 0

    proposed = propose_successor_mutation(db, store, cycle, _manifest(cycle.version_id))
    assert proposed.state == "MUTATION_PROPOSED"
    assert proposed.successor_mutation_allowed is False
    assert proposed.mutation is not None
    with pytest.raises(FrozenInstanceError):
        proposed.mutation.hypothesis = "second mutation"  # type: ignore[misc]
    with pytest.raises(SuccessorMutationError):
        propose_successor_mutation(db, store, cycle, _manifest(cycle.version_id))
    with pytest.raises(SuccessorMutationError):
        run_development_cycle(
            db,
            store,
            snapshot,
            code_commit=COMMIT,
            family_id=cycle.family_id,
            prior_version_id=cycle.version_id,
            policy=POLICY,
            starting_equity=STARTING_EQUITY,
        )

    successor = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        family_id=cycle.family_id,
        parameters=StrategyParameters(hourly_ema_lookback=22),
        prior_version_id=cycle.version_id,
        mutation=proposed.mutation,
        prior_result=cycle.result,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert successor.artifacts is not None
    assert successor.artifacts.prior_version_id == cycle.version_id
    assert successor.artifacts.mutation_count == 1
    assert successor.ledger is not None
    assert successor.ledger.prior_version_comparison is not None
    assert successor.ledger.prior_version_comparison["prior_version_id"] == cycle.version_id
    assert db.get_budget(cycle.family_id).performance_evaluated_versions == 2
    assert db.get_budget(cycle.family_id).oos_evaluations == 0


def test_development_pass_automatically_runs_sealed_oos(tmp_path: Path) -> None:
    db, store, snapshot = _harness(tmp_path)
    sealed = SealedOos(_oos_artifacts(profitable=True), path="C:/secret/oos.parquet", credentials="token")
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
        sealed_oos=sealed,
        _evaluate_dev=_pass_development,
    )
    assert cycle.result is not None
    assert cycle.result.outcome is GateResult.PASS
    assert cycle.oos is not None
    assert cycle.oos.result is not None
    assert cycle.oos.result.kind == "oos"
    assert cycle.oos.state == "READY_FOR_PAPER"
    assert cycle.oos.ledger is not None
    assert cycle.oos.ledger.mutation is None
    assert cycle.oos.ledger.mutation_hypothesis is None
    assert db.get_budget(cycle.family_id).performance_evaluated_versions == 1
    assert db.get_budget(cycle.family_id).oos_evaluations == 1
    states = [row["to_state"] for row in db.list_transitions(cycle.run.run_id)]
    assert states == ["DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING", "READY_FOR_PAPER"]
    with pytest.raises(TransitionError):
        run_sealed_oos(db, store, run=cycle.run, sealed=sealed)


def test_oos_technical_failure_retries_in_place_without_second_budget(tmp_path: Path) -> None:
    db, store, snapshot = _harness(tmp_path)
    ok = _oos_artifacts(profitable=True)
    failed = SealedOos(replace(ok, technical_error=True, technical_kind="RUN_ERROR"))
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
        sealed_oos=failed,
        _evaluate_dev=_pass_development,
    )
    assert cycle.state == "OOS_RUNNING"
    assert cycle.oos is not None
    assert cycle.oos.technical_kind == "RUN_ERROR"
    assert cycle.oos.result is None
    assert db.get_budget(cycle.family_id).oos_evaluations == 1
    assert [row["to_state"] for row in db.list_transitions(cycle.run.run_id)] == [
        "DRAFT",
        "DEVELOPMENT_RUNNING",
        "OOS_RUNNING",
    ]

    retried = run_sealed_oos(db, store, run=cycle.run, sealed=SealedOos(ok))
    assert retried.state == "READY_FOR_PAPER"
    assert retried.result is not None
    assert retried.result.kind == "oos"
    assert retried.technical_kind is None
    assert db.get_budget(cycle.family_id).oos_evaluations == 1
    assert [row["to_state"] for row in db.list_transitions(cycle.run.run_id)] == [
        "DRAFT",
        "DEVELOPMENT_RUNNING",
        "OOS_RUNNING",
        "READY_FOR_PAPER",
    ]
    with pytest.raises(TransitionError):
        run_sealed_oos(db, store, run=cycle.run, sealed=SealedOos(ok))


def test_ledger_has_gates_attribution_comparison_and_no_oos_mutation(tmp_path: Path) -> None:
    db, store, snapshot = _harness(tmp_path)
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.ledger is not None
    assert cycle.result is not None
    failed = tuple(name for name, gate in cycle.result.gates.items() if gate is GateResult.FAIL)
    achieved = tuple(name for name, gate in cycle.result.gates.items() if gate is GateResult.PASS)
    assert cycle.ledger.gates_failed == failed
    assert cycle.ledger.gates_achieved == achieved
    assert cycle.ledger.executive_summary
    attribution = cycle.ledger.loss_attribution
    for key in ("by_symbol", "by_direction", "by_regime", "by_period", "by_exit_reason", "by_cost_type"):
        assert key in attribution
    assert set(attribution["by_cost_type"]) >= {"fees", "funding", "slippage"}
    assert cycle.ledger.run_id == cycle.run.run_id
    assert cycle.ledger.trade_references
    assert cycle.ledger.mutation_hypothesis
    assert cycle.result.outcome is GateResult.FAIL

    proposed = propose_successor_mutation(db, store, cycle, _manifest(cycle.version_id))
    successor = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        family_id=cycle.family_id,
        prior_version_id=cycle.version_id,
        mutation=proposed.mutation,
        prior_result=cycle.result,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert successor.ledger is not None
    assert successor.ledger.prior_version_comparison is not None
    assert "profit_factor" in successor.ledger.prior_version_comparison

    oos = run_development_cycle(
        Database(tmp_path / "oos.sqlite3"),
        ArtifactStore(tmp_path / "oos-artifacts"),
        snapshot,
        code_commit=COMMIT,
        sealed_oos=SealedOos(_oos_artifacts(profitable=False)),
        _evaluate_dev=_pass_development,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert oos.oos is not None
    assert oos.oos.state == "REJECTED"
    assert oos.oos.ledger is not None
    assert oos.oos.ledger.mutation is None
    assert oos.oos.ledger.mutation_hypothesis is None
    assert oos.oos.ledger.gates_failed or oos.oos.ledger.gates_achieved
    with pytest.raises(ValueError, match="mutation"):
        build_ledger_bundle(
            kind="oos",
            result=oos.oos.result,
            run_id=oos.run.run_id,
            version_id=oos.version_id,
            trades=oos.oos.artifacts.trades if oos.oos.artifacts else (),
            start=DEV_END,
            end=OOS_END,
            mutation_hypothesis="do not propose from OOS",
        )


def test_oos_artifacts_are_analyzable_but_excluded_from_research_and_coding_inputs(
    tmp_path: Path,
) -> None:
    db, store, snapshot = _harness(tmp_path)
    sealed = SealedOos(_oos_artifacts(profitable=False), path="/sealed/oos", credentials="x")
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        sealed_oos=sealed,
        _evaluate_dev=_pass_development,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.oos is not None
    assert cycle.oos.ledger is not None
    assert cycle.oos.result is not None
    assert store.path_for(cycle.oos.artifact_digests["ledger"]).exists()
    assert store.path_for(cycle.oos.artifact_digests["result_bundle"]).exists()
    payload = research_inputs(development=cycle.result, ledger=cycle.ledger.to_payload())
    assert "oos" not in payload
    coding = coding_inputs(development=cycle.result, mutation=cycle.mutation, worktree=WORKTREE)
    assert "oos" not in coding
    with pytest.raises(ValueError, match="OOS"):
        research_inputs(development=cycle.result, oos=cycle.oos.result)
    with pytest.raises(ValueError, match="OOS"):
        coding_inputs({"oos_trades": []}, worktree=WORKTREE)
    with pytest.raises(ValueError, match="OOS"):
        coding_inputs(sealed, worktree=WORKTREE)
    analyzed = build_ledger_bundle(
        kind="oos",
        result=cycle.oos.result,
        run_id=cycle.run.run_id,
        version_id=cycle.version_id,
        trades=cycle.oos.artifacts.trades if cycle.oos.artifacts else (),
        start=DEV_END,
        end=OOS_END,
    )
    assert analyzed.mutation is None
    assert analyzed.kind == "oos"


def test_research_loop_does_not_pass_db_into_evaluate_helpers() -> None:
    source = Path("src/trading_desk/workflows/research_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {"evaluate_development", "evaluate_oos_once"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in banned:
            assert "db" not in {keyword.arg for keyword in node.keywords}


def test_mutation_manifest_is_immutable_and_hashed() -> None:
    manifest = _manifest("abc")
    assert len(manifest.manifest_hash) == 64
    assert manifest.manifest_hash == sha256_hex(canonical_json(manifest.to_payload()))
    with pytest.raises(FrozenInstanceError):
        manifest.files_changed = ("other.py",)  # type: ignore[misc]


def test_trade_records_include_slippage_cost(tmp_path: Path) -> None:
    db, store, snapshot = _harness(tmp_path)
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.artifacts is not None
    assert cycle.artifacts.trades
    assert all(row.slippage_cost > 0 for row in cycle.artifacts.trades)
    assert cycle.result is not None
    from trading_desk.validation.metrics import as_decimal, stressed_net_pnl

    stress = EvaluationPolicy.v2().document["development"]["execution_stress"]
    stressed = [stressed_net_pnl(row, stress) for row in cycle.artifacts.trades]
    assert stressed != [row.net_pnl for row in cycle.artifacts.trades]
    assert as_decimal(cycle.result.metrics["profit_factor_stress"]) <= as_decimal(
        cycle.result.metrics["profit_factor"]
    ) or as_decimal(cycle.result.metrics["profit_factor"]).is_infinite()


def test_data_blocked_does_not_consume_performance_budget(tmp_path: Path) -> None:
    db, store, snapshot = _harness(tmp_path)
    blocked = replace(snapshot, klines_1m=(), hourly_bars=())
    cycle = run_development_cycle(
        db,
        store,
        blocked,
        code_commit=COMMIT,
        policy=POLICY,
        starting_equity=STARTING_EQUITY,
    )
    assert cycle.state == "DATA_BLOCKED"
    assert cycle.technical_kind == "DATA_BLOCKED"
    assert db.get_budget(cycle.family_id).performance_evaluated_versions == 0
    assert db.get_budget(cycle.family_id).oos_evaluations == 0
