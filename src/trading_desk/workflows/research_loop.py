from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Sequence

from trading_desk.backtest.execution import BacktestEngine, BacktestResult, Trade
from trading_desk.config import SUPPORTED_SYMBOLS, canonical_json, sha256_hex
from trading_desk.data.aggregate import derive_hourly_bars
from trading_desk.data.contracts import (
    DATA_BLOCKED,
    DAY,
    MINUTE,
    OK,
    DataSnapshot,
    derived_data_hash,
    source_hash,
)
from trading_desk.data.validate import validate_snapshot
from trading_desk.agents.capabilities import coding_inputs, research_inputs
from trading_desk.ledger.bundle import (
    LedgerBundle,
    MutationManifest,
    build_ledger_bundle,
    trade_payload,
)
from trading_desk.state.db import Database, RunIdentity
from trading_desk.state.transitions import TransitionError, transition
from trading_desk.storage.artifacts import ArtifactStore
from trading_desk.strategy.default import DefaultStrategy, regime
from trading_desk.strategy.models import (
    LONG,
    ExecutionPolicy,
    StrategyParameters,
    make_strategy_version,
)
from trading_desk.validation.gates import (
    EvaluationPolicy,
    GateResult,
    ResultBundle,
    TechnicalEvaluationError,
    evaluate_development,
)
from trading_desk.validation.metrics import (
    EvaluationArtifacts,
    SurvivalCase,
    TradeRecord,
    equity_at,
    max_drawdown,
    perturbed_parameters,
    profit_factor,
    reconstruct_equity_curve,
    total_return,
)
from trading_desk.validation.oos import SealedOos, evaluate_oos_once

ONE = Decimal("1")
EvaluateDev = Callable[..., ResultBundle]
EvaluateOos = Callable[..., ResultBundle]


class ResearchLoopError(ValueError):
    """Fail-closed research-loop error."""


class SuccessorMutationError(ResearchLoopError):
    """Successor versions must declare exactly one mutation."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    family_id: str
    version_id: str
    run: RunIdentity
    state: str
    backtest: BacktestResult | None = None
    artifacts: EvaluationArtifacts | None = None
    result: ResultBundle | None = None
    ledger: LedgerBundle | None = None
    artifact_digests: dict[str, str] = field(default_factory=dict)
    analysis_requested: bool = False
    successor_mutation_allowed: bool = False
    mutation: MutationManifest | None = None
    oos: CycleResult | None = None
    technical_kind: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_digests", dict(self.artifact_digests))


class _SkipSymbolStrategy:
    def __init__(self, inner: DefaultStrategy, skipped: str) -> None:
        self._inner = inner
        self.parameters = inner.parameters
        self.family = inner.family
        self._skipped = skipped

    def signal(self, symbol: str, hour_bar: Any, hourly_closes: Sequence, daily_closes: Sequence) -> Any:
        if symbol == self._skipped:
            return None
        return self._inner.signal(symbol, hour_bar, hourly_closes, daily_closes)


def _unslipped(slipped: Decimal, rate: Decimal, *, buy: bool) -> Decimal:
    if rate == 0:
        return slipped
    if buy:
        return slipped / (ONE + rate)
    return slipped / (ONE - rate)


def trade_to_record(trade: Trade, policy: ExecutionPolicy, *, regime_name: str = "") -> TradeRecord:
    entry_buy = trade.direction == LONG
    unslipped_entry = _unslipped(trade.entry_price, policy.slippage_rate, buy=entry_buy)
    unslipped_exit = _unslipped(trade.exit_price, policy.slippage_rate, buy=not entry_buy)
    if trade.direction == LONG:
        unslipped_gross = (unslipped_exit - unslipped_entry) * trade.quantity
    else:
        unslipped_gross = (unslipped_entry - unslipped_exit) * trade.quantity
    return TradeRecord(
        symbol=trade.symbol,
        direction=trade.direction,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        net_pnl=trade.net_pnl,
        fees=trade.fees,
        funding=trade.funding,
        slippage_cost=unslipped_gross - trade.gross_pnl,
        gross_pnl=trade.gross_pnl,
        exit_reason=trade.exit_reason,
        regime=regime_name,
    )


def _regime_at(snapshot: DataSnapshot, symbol: str, when: datetime) -> str:
    closes = [
        bar.close
        for bar in snapshot.daily_bars
        if bar.symbol == symbol and bar.open_time + DAY <= when
    ]
    return regime(closes)


def _span(snapshot: DataSnapshot) -> tuple[datetime, datetime]:
    start = snapshot.evaluation_start
    if start is None:
        raise ResearchLoopError("evaluation_start is required")
    if not snapshot.klines_1m:
        return start, start
    end = max(bar.open_time for bar in snapshot.klines_1m) + MINUTE
    return start, end


def _snapshot_hashes(snapshot: DataSnapshot) -> tuple[str, str]:
    source = snapshot.source_hash or source_hash(snapshot.klines_1m, snapshot.funding, snapshot.metadata)
    hourly = snapshot.hourly_bars
    if not hourly:
        derived = derive_hourly_bars(snapshot.klines_1m, source_hash=source)
        hourly = derived.bars if derived.status == OK else ()
    derived = snapshot.derived_data_hash or derived_data_hash(tuple(hourly) + tuple(snapshot.daily_bars))
    if not derived:
        derived = sha256_hex(canonical_json({"source_hash": source}))
    return source, derived


def _step(db: Database, run_id: str, to_state: str, reason: str | None = None) -> str:
    return transition(
        db,
        run_id=run_id,
        to_state=to_state,
        idempotency_key=f"{run_id}:{to_state}",
        reason=reason,
    )


def _current_state(db: Database, run_id: str) -> str | None:
    rows = db.list_transitions(run_id)
    if not rows:
        return None
    return str(rows[-1]["to_state"])


def _survival_case(
    result: BacktestResult,
    start: datetime,
    end: datetime,
    starting: Decimal,
    policy: ExecutionPolicy,
    *,
    left_out: str | None = None,
    parameter: str | None = None,
) -> SurvivalCase:
    records = tuple(trade_to_record(row, policy) for row in result.trades)
    artifacts = EvaluationArtifacts(trades=records, starting_equity=starting, start=start, end=end)
    curve = reconstruct_equity_curve(artifacts)
    start_eq = equity_at(curve, start, starting)
    end_eq = equity_at(curve, end, starting)
    return SurvivalCase(
        total_return=total_return(start_eq, end_eq),
        profit_factor=profit_factor([row.net_pnl for row in records]),
        max_drawdown=max_drawdown([point.equity for point in curve] or [start_eq]),
        parameter=parameter,
        left_out_symbol=left_out,
    )


def _flatten_open_positions(engine: BacktestEngine, snapshot: DataSnapshot) -> None:
    if not engine.account.positions:
        return
    last_time = max(bar.open_time for bar in snapshot.klines_1m)
    symbol = next(iter(engine.account.positions))
    position = engine.account.positions[symbol]
    mark = engine._marks.get(symbol, position.entry_price)
    rows = engine._metadata.get(symbol, [])
    meta = None
    for row in rows:
        if row.effective_from <= last_time:
            meta = row
    if meta is None:
        return
    engine._flatten_all(last_time, symbol, meta, mark, "end_of_window")


def _result_from_engine(engine: BacktestEngine, status: str, reasons: tuple[str, ...]) -> BacktestResult:
    ending = engine.account.equity(engine._marks)
    return BacktestResult(
        status=status,
        reasons=reasons,
        trades=tuple(engine.trades),
        fills=tuple(engine.fills),
        rejected=tuple(engine.rejected),
        ending_equity=ending,
        max_drawdown=engine.account.max_drawdown,
        equity_high_water=engine.account.high_water,
        halted=engine.account.halted,
        daily_paused=engine.account.daily_paused,
        funding_pnl=engine.account.funding_pnl,
    )


def _run_engine(
    snapshot: DataSnapshot,
    strategy: Any,
    policy: ExecutionPolicy,
    starting: Decimal,
) -> BacktestResult:
    engine = BacktestEngine()
    result = engine.run(snapshot, strategy, policy, starting)
    if result.status == DATA_BLOCKED:
        return result
    _flatten_open_positions(engine, snapshot)
    return _result_from_engine(engine, result.status, result.reasons)


def _changed_parameter(base: StrategyParameters, other: StrategyParameters) -> str | None:
    if base.hourly_ema_lookback != other.hourly_ema_lookback:
        return "hourly_ema_lookback"
    if base.stop_pct != other.stop_pct:
        return "stop_pct"
    if base.take_profit_r != other.take_profit_r:
        return "take_profit_r"
    return None


def _persist(
    store: ArtifactStore,
    *,
    result: ResultBundle,
    ledger: LedgerBundle,
    trades: Sequence[TradeRecord],
) -> dict[str, str]:
    return {
        "ledger": store.put_json(ledger.to_payload()),
        "result_bundle": store.put_json(result.to_payload()),
        "trades": store.put_json([trade_payload(row) for row in trades]),
    }


def _require_single_mutation(
    *,
    prior_version_id: str | None,
    mutation: MutationManifest | None,
) -> tuple[str | None, MutationManifest | None]:
    if prior_version_id is not None and mutation is None:
        raise SuccessorMutationError("successor requires exactly one mutation")
    if mutation is None:
        return prior_version_id, None
    if not mutation.hypothesis.strip():
        raise SuccessorMutationError("mutation hypothesis is required")
    if not mutation.files_changed and not mutation.configuration_fields_changed:
        raise SuccessorMutationError("mutation must declare files or configuration fields")
    if prior_version_id is None:
        prior_version_id = mutation.prior_version_id
    if mutation.prior_version_id != prior_version_id:
        raise SuccessorMutationError("mutation prior_version_id mismatch")
    return prior_version_id, mutation


def run_sealed_oos(
    db: Database,
    store: ArtifactStore,
    *,
    run: RunIdentity,
    sealed: SealedOos | EvaluationArtifacts,
    prior_result: ResultBundle | None = None,
    prior_version_id: str | None = None,
    _evaluate_oos_fn: EvaluateOos | None = None,
) -> CycleResult:
    current = _current_state(db, run.run_id)
    if current == "DEVELOPMENT_RUNNING":
        _step(db, run.run_id, "OOS_RUNNING")
    elif current != "OOS_RUNNING":
        raise TransitionError("illegal transition")
    policy = EvaluationPolicy.v2()
    try:
        if _evaluate_oos_fn is None:
            bundle = evaluate_oos_once(sealed, policy)
        else:
            bundle = _evaluate_oos_fn(sealed, policy)
    except TechnicalEvaluationError as exc:
        return CycleResult(
            family_id=run.family_id,
            version_id=run.strategy_version_id,
            run=run,
            state="OOS_RUNNING",
            technical_kind=str(exc) or "RUN_ERROR",
        )
    artifacts = sealed.artifacts_for_authorized_run() if isinstance(sealed, SealedOos) else sealed
    dest = "READY_FOR_PAPER" if bundle.outcome is GateResult.PASS else "REJECTED"
    state = _step(db, run.run_id, dest)
    ledger = build_ledger_bundle(
        kind="oos",
        result=bundle,
        run_id=run.run_id,
        version_id=run.strategy_version_id,
        trades=artifacts.trades,
        start=artifacts.start,
        end=artifacts.end,
        prior_result=prior_result,
        prior_version_id=prior_version_id,
    )
    digests = _persist(store, result=bundle, ledger=ledger, trades=artifacts.trades)
    return CycleResult(
        family_id=run.family_id,
        version_id=run.strategy_version_id,
        run=run,
        state=state,
        artifacts=artifacts,
        result=bundle,
        ledger=ledger,
        artifact_digests=digests,
    )


def propose_successor_mutation(
    db: Database,
    store: ArtifactStore,
    cycle: CycleResult,
    manifest: MutationManifest,
) -> CycleResult:
    current = _current_state(db, cycle.run.run_id)
    if current != "ANALYSIS_READY":
        raise SuccessorMutationError("successor mutation already proposed")
    if cycle.result is None or cycle.result.outcome is not GateResult.FAIL:
        raise SuccessorMutationError("mutation is only allowed after development failure")
    prior_version_id, manifest = _require_single_mutation(
        prior_version_id=cycle.version_id,
        mutation=manifest,
    )
    assert manifest is not None
    _step(db, cycle.run.run_id, "MUTATION_PROPOSED")
    digest = store.put_json(manifest.to_payload())
    digests = dict(cycle.artifact_digests)
    digests["mutation"] = digest
    return replace(
        cycle,
        state="MUTATION_PROPOSED",
        successor_mutation_allowed=False,
        mutation=manifest,
        artifact_digests=digests,
    )


def run_development_cycle(
    db: Database,
    store: ArtifactStore,
    snapshot: DataSnapshot,
    *,
    code_commit: str,
    family_id: str | None = None,
    parameters: StrategyParameters | None = None,
    policy: ExecutionPolicy | None = None,
    starting_equity: Decimal | None = None,
    prior_version_id: str | None = None,
    mutation: MutationManifest | None = None,
    prior_result: ResultBundle | None = None,
    sealed_oos: SealedOos | EvaluationArtifacts | None = None,
    evaluation_artifacts: EvaluationArtifacts | None = None,
    _evaluate_dev: EvaluateDev | None = None,
    _evaluate_oos_fn: EvaluateOos | None = None,
) -> CycleResult:
    policy = policy or ExecutionPolicy()
    parameters = parameters or StrategyParameters()
    starting = starting_equity if starting_equity is not None else Decimal("10000")
    validation_policy = EvaluationPolicy.v2()
    prior_version_id, mutation = _require_single_mutation(
        prior_version_id=prior_version_id,
        mutation=mutation,
    )
    family_id = family_id or db.create_family()
    version = make_strategy_version(
        code_commit=code_commit,
        parameters=parameters,
        family_id=family_id,
    )
    db.register_version(
        family_id,
        code_commit=code_commit,
        spec=parameters.as_mapping(),
        strategy_version_id=version.strategy_version_id,
    )
    source, derived = _snapshot_hashes(snapshot)
    identity = db.create_run(
        family_id=family_id,
        strategy_version_id=version.strategy_version_id,
        code_commit=code_commit,
        data_snapshot_hash=source if len(source) == 64 else sha256_hex(source),
        derived_data_hash=derived if len(derived) == 64 else sha256_hex(derived),
        validation_policy_hash=validation_policy.policy_hash,
        execution_policy_hash=policy.policy_hash,
    )
    _step(db, identity.run_id, "DRAFT")
    _step(db, identity.run_id, "DEVELOPMENT_RUNNING")

    checked = validate_snapshot(snapshot)
    if checked.status != OK or checked.snapshot is None:
        state = _step(db, identity.run_id, "DATA_BLOCKED", reason="; ".join(checked.reasons))
        return CycleResult(
            family_id=family_id,
            version_id=version.strategy_version_id,
            run=identity,
            state=state,
            technical_kind="DATA_BLOCKED",
        )
    validated = checked.snapshot

    try:
        backtest = _run_engine(validated, DefaultStrategy(parameters), policy, starting)
    except Exception as exc:
        _step(db, identity.run_id, "RUN_ERROR", reason=str(exc))
        raise

    if backtest.status == DATA_BLOCKED:
        state = _step(db, identity.run_id, "DATA_BLOCKED", reason="; ".join(backtest.reasons))
        return CycleResult(
            family_id=family_id,
            version_id=version.strategy_version_id,
            run=identity,
            state=state,
            backtest=backtest,
            technical_kind="DATA_BLOCKED",
        )

    start, end = _span(validated)
    records = tuple(
        trade_to_record(
            trade,
            policy,
            regime_name=_regime_at(validated, trade.symbol, trade.entry_time),
        )
        for trade in backtest.trades
    )
    budget = db.get_budget(family_id)
    if evaluation_artifacts is None:
        strategy = DefaultStrategy(parameters)
        neighborhood = []
        for perturbed in perturbed_parameters(parameters):
            result = _run_engine(validated, DefaultStrategy(perturbed), policy, starting)
            neighborhood.append(
                _survival_case(
                    result,
                    start,
                    end,
                    starting,
                    policy,
                    parameter=_changed_parameter(parameters, perturbed),
                )
            )
        leave_one_out = []
        for symbol in SUPPORTED_SYMBOLS:
            result = _run_engine(
                validated,
                _SkipSymbolStrategy(strategy, symbol),
                policy,
                starting,
            )
            leave_one_out.append(
                _survival_case(result, start, end, starting, policy, left_out=symbol)
            )
        artifacts = EvaluationArtifacts(
            trades=records,
            starting_equity=starting,
            start=start,
            end=end,
            neighborhood=tuple(neighborhood),
            leave_one_out=tuple(leave_one_out),
            mutation_count=1 if mutation is not None else 0,
            prior_version_id=prior_version_id,
            family_id=family_id,
            strategy_version_id=version.strategy_version_id,
            performance_evaluated_versions=budget.performance_evaluated_versions + 1,
            parameters=parameters,
        )
    else:
        artifacts = replace(
            evaluation_artifacts,
            mutation_count=1 if mutation is not None else evaluation_artifacts.mutation_count,
            prior_version_id=prior_version_id if prior_version_id is not None else evaluation_artifacts.prior_version_id,
            family_id=family_id,
            strategy_version_id=version.strategy_version_id,
            performance_evaluated_versions=budget.performance_evaluated_versions + 1,
            parameters=evaluation_artifacts.parameters or parameters,
        )
        if not artifacts.trades and records:
            artifacts = replace(artifacts, trades=records)

    try:
        if _evaluate_dev is None:
            bundle = evaluate_development(artifacts, validation_policy)
        else:
            bundle = _evaluate_dev(artifacts, validation_policy)
    except TechnicalEvaluationError as exc:
        dest = "DATA_BLOCKED" if "DATA_BLOCKED" in str(exc) else "RUN_ERROR"
        state = _step(db, identity.run_id, dest, reason=str(exc))
        return CycleResult(
            family_id=family_id,
            version_id=version.strategy_version_id,
            run=identity,
            state=state,
            backtest=backtest,
            artifacts=artifacts,
            technical_kind=dest,
        )

    ledger_trades = artifacts.trades
    if bundle.outcome is GateResult.FAIL:
        state = _step(db, identity.run_id, "ANALYSIS_READY")
        ledger = build_ledger_bundle(
            kind="development",
            result=bundle,
            run_id=identity.run_id,
            version_id=version.strategy_version_id,
            trades=ledger_trades,
            start=artifacts.start,
            end=artifacts.end,
            prior_result=prior_result,
            prior_version_id=prior_version_id,
            mutation=mutation,
        )
        digests = _persist(store, result=bundle, ledger=ledger, trades=ledger_trades)
        db.enqueue_outbox(
            topic="analysis_request",
            payload={
                "kind": "development",
                "result_bundle_hash": bundle.bundle_hash,
                "run_id": identity.run_id,
                "strategy_version_id": version.strategy_version_id,
            },
            idempotency_key=f"{identity.run_id}:analysis-request",
        )
        return CycleResult(
            family_id=family_id,
            version_id=version.strategy_version_id,
            run=identity,
            state=state,
            backtest=backtest,
            artifacts=artifacts,
            result=bundle,
            ledger=ledger,
            artifact_digests=digests,
            analysis_requested=True,
            successor_mutation_allowed=True,
            mutation=mutation,
        )

    ledger = build_ledger_bundle(
        kind="development",
        result=bundle,
        run_id=identity.run_id,
        version_id=version.strategy_version_id,
        trades=ledger_trades,
        start=artifacts.start,
        end=artifacts.end,
        prior_result=prior_result,
        prior_version_id=prior_version_id,
    )
    digests = _persist(store, result=bundle, ledger=ledger, trades=ledger_trades)
    if sealed_oos is None:
        raise ResearchLoopError("sealed OOS is required after development pass")
    oos = run_sealed_oos(
        db,
        store,
        run=identity,
        sealed=sealed_oos,
        prior_result=bundle,
        prior_version_id=version.strategy_version_id,
        _evaluate_oos_fn=_evaluate_oos_fn,
    )
    return CycleResult(
        family_id=family_id,
        version_id=version.strategy_version_id,
        run=identity,
        state=oos.state,
        backtest=backtest,
        artifacts=artifacts,
        result=bundle,
        ledger=ledger,
        artifact_digests=digests,
        mutation=mutation,
        oos=oos,
    )
