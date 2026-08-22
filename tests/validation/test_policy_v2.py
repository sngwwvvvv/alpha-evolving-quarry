from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_desk.config import SUPPORTED_SYMBOLS, UTC, canonical_json, sha256_hex
from trading_desk.state.db import Database
from trading_desk.strategy.models import StrategyParameters
from trading_desk.validation import (
    BudgetExhausted,
    EquityPoint,
    EvaluationArtifacts,
    EvaluationPolicy,
    GateResult,
    ResultBundle,
    SealedOos,
    SealedOosError,
    SurvivalCase,
    TechnicalEvaluationError,
    TradeRecord,
    evaluate_development,
    evaluate_oos_once,
    research_inputs,
)
from trading_desk.validation.gates import evaluate_gates
from trading_desk.validation.metrics import compute_metrics, perturbed_parameters, window_equities
from trading_desk.validation.statistics import annualized_psr_benchmark, probabilistic_sharpe_ratio
from trading_desk.validation.walk_forward import in_half_open, walk_forward_windows

GOLDEN_BUNDLE_HASH = "2eb0b1dc22205c5722e78fb7dbdeecd04cddea09cbe4c6d6d7a6c7e03f4564e8"
START = datetime(2020, 1, 1, tzinfo=UTC)
END = datetime(2021, 1, 1, tzinfo=UTC)
OOS_END = datetime(2021, 7, 1, tzinfo=UTC)


def passing_dev_metrics() -> dict[str, object]:
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


def passing_oos_metrics() -> dict[str, object]:
    return {
        "trade_count": 40,
        "total_return": Decimal("0.05"),
        "profit_factor": Decimal("1.20"),
        "max_drawdown": Decimal("0.10"),
        "sealed_window_months": 6,
        "psr": Decimal("0.80"),
    }


def _trade(
    symbol: str,
    day: int,
    net_pnl: str,
    *,
    direction: str = "LONG",
    fees: str = "0",
    funding: str = "0",
    slippage_cost: str = "0",
    month: int = 1,
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
    )


def _survival(ok: bool = True) -> SurvivalCase:
    if ok:
        return SurvivalCase(
            total_return=Decimal("0.01"),
            profit_factor=Decimal("1.10"),
            max_drawdown=Decimal("0.10"),
        )
    return SurvivalCase(
        total_return=Decimal("-0.01"),
        profit_factor=Decimal("0.90"),
        max_drawdown=Decimal("0.25"),
    )


def _artifacts(*, trades: tuple[TradeRecord, ...] | None = None) -> EvaluationArtifacts:
    rows = trades or tuple(
        _trade(symbol, 1 + i, "1") for i, symbol in enumerate(SUPPORTED_SYMBOLS)
    )
    return EvaluationArtifacts(
        trades=rows,
        starting_equity=Decimal("10000"),
        start=START,
        end=END,
        neighborhood=tuple(_survival() for _ in range(6)),
        leave_one_out=tuple(_survival() for _ in range(4)),
        performance_evaluated_versions=1,
        parameters=StrategyParameters(),
    )


def test_policy_v2_values_match_spec() -> None:
    policy = EvaluationPolicy.v2()
    doc = policy.document
    assert policy.version == "validation-policy-v2"
    assert doc["version"] == "validation-policy-v2"
    boot = doc["development"]["profit_factor"]["bootstrap"]
    assert boot["seed"] == 20260823
    assert boot["block_days"] == 30
    assert boot["resamples"] == 5000
    assert boot["algorithm"] == "trade-entry-moving-block-v1"
    assert boot["preserve"] == ["whole_trades", "cross_symbol_entry_clusters"]
    assert boot["confidence"] == 0.90
    assert boot["gate_lower_bound_exclusive_minimum"] == 1.00
    assert boot["target_lower_bound_minimum"] == 1.05
    mdd = doc["development"]["risk"]["max_drawdown"]
    assert mdd["gate_exclusive_maximum"] == 0.15
    assert mdd["target_maximum"] == 0.10
    assert mdd["scope"] == ["aggregate", "each_walk_forward_window"]
    wf = doc["development"]["walk_forward"]
    assert wf["window_months"] == 6
    assert wf["overlap"] is False
    assert wf["positive_window_fraction"]["gate_minimum"] == 0.60
    assert doc["development"]["profitability"]["cagr"]["gate_minimum"] == 0.10
    assert doc["development"]["profitability"]["calmar"]["gate_minimum"] == 0.75
    assert doc["development"]["trades"]["aggregate"]["gate_minimum"] == 120
    assert doc["development"]["trades"]["per_symbol"]["gate_minimum"] == 20
    pf = doc["development"]["profit_factor"]
    assert pf["base_point_estimate"]["gate_minimum"] == 1.20
    assert pf["stress_point_estimate"]["gate_minimum"] == 1.05
    stress = doc["development"]["execution_stress"]
    assert stress == {
        "fee_multiplier": 1.5,
        "slippage_multiplier": 2.0,
        "adverse_paid_funding_multiplier": 1.5,
        "received_funding_multiplier": 0.5,
    }
    psr = doc["development"]["statistical_confidence"]["psr"]
    assert psr["return_frequency"] == "weekly_utc"
    assert psr["benchmark_sharpe"] == 0.50
    assert psr["gate_probability_minimum"] == 0.90
    dsr = doc["development"]["statistical_confidence"]["dsr"]
    assert dsr["gate_probability_minimum"] == 0.95
    assert dsr["trial_count"] == "every_performance_evaluated_version"
    assert dsr["correlation_treatment"] == "daily-psr-dsr-v1"
    conc = doc["development"]["concentration"]
    assert conc["symbol"]["gate_share_maximum"] == 0.70
    assert conc["direction"]["gate"] == "report_only"
    assert conc["period"]["bucket"] == "walk_forward_window"
    assert conc["period"]["gate_share_maximum"] == 0.70
    nb = doc["development"]["neighborhood"]
    assert nb["perturbation"] == "one_parameter_at_a_time_plus_minus_10_percent"
    assert nb["survival"] == {
        "total_return_minimum": 0.00,
        "profit_factor_minimum": 1.00,
        "max_drawdown_maximum": 0.20,
    }
    assert nb["gate_survival_fraction_minimum"] == 0.50
    loo = doc["development"]["leave_one_symbol_out"]
    assert loo["cases"] == 4
    assert loo["gate_surviving_count_minimum"] == 3
    oos = doc["oos"]
    assert oos["sealed_window_months"] == 6
    assert oos["evaluations_per_family"] == 1
    assert oos["trades"]["gate_minimum"] == 20
    assert oos["total_return"]["gate_exclusive_minimum"] == 0.00
    assert oos["profit_factor"]["gate_minimum"] == 1.05
    assert oos["max_drawdown"]["gate_exclusive_maximum"] == 0.15
    assert oos["psr"] == "report_only"
    assert oos["dsr"] == "not_recomputed"
    assert oos["bootstrap"] == "not_run"
    assert oos["neighborhood"] == "not_run"
    assert oos["leave_one_symbol_out"] == "not_run"
    budget = doc["budget"]
    assert budget["max_performance_evaluated_versions_per_family"] == 8
    assert budget["mutations_per_successor_version"] == 1
    assert budget["max_oos_evaluations_per_family"] == 1
    assert budget["technical_errors_consume_budget"] is False
    assert len(policy.policy_hash) == 64
    assert policy.policy_hash == sha256_hex(canonical_json(EvaluationPolicy._jsonable(doc)))


def test_hard_boundaries_and_independent_gates() -> None:
    policy = EvaluationPolicy.v2()
    base = passing_dev_metrics()
    gates = evaluate_gates(base, policy, kind="development")
    assert set(gates.values()) == {GateResult.PASS}
    assert "concentration_direction" not in gates
    assert gates.keys() == {
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

    violations: dict[str, dict[str, object]] = {
        "cagr": {"cagr": Decimal("0.099999")},
        "calmar": {"calmar": Decimal("0.749999")},
        "max_drawdown_aggregate": {"max_drawdown": Decimal("0.15")},
        "max_drawdown_windows": {"window_max_drawdowns": (Decimal("0.15"), Decimal("0.01"))},
        "positive_window_fraction": {"positive_window_fraction": Decimal("0")},
        "trades_aggregate": {"trade_count": 119},
        "trades_per_symbol": {
            "trade_count_by_symbol": {
                "BTCUSDT": 19,
                "ETHUSDT": 50,
                "XRPUSDT": 50,
                "SOLUSDT": 50,
            }
        },
        "profit_factor_base": {"profit_factor": Decimal("1.199999")},
        "profit_factor_stress": {"profit_factor_stress": Decimal("1.049999")},
        "profit_factor_bootstrap": {
            "profit_factor_bootstrap_lower_bound": Decimal("1.00")
        },
        "psr": {"psr": Decimal("0.899999")},
        "dsr": {"dsr": Decimal("0.949999")},
        "concentration_symbol": {"concentration_symbol_max": Decimal("0.700001")},
        "concentration_period": {"concentration_period_max": Decimal("0.700001")},
        "neighborhood": {"neighborhood_survival_fraction": Decimal("0.499999")},
        "leave_one_symbol_out": {"leave_one_out_surviving_count": 2},
        "mutations_per_successor": {
            "has_prior_version": True,
            "mutation_count": 2,
        },
    }
    for failed_name, patch in violations.items():
        metrics = {**base, **patch}
        result = evaluate_gates(metrics, policy, kind="development")
        assert result[failed_name] is GateResult.FAIL, failed_name
        others = {name for name in result if name != failed_name}
        assert {result[name] for name in others} == {GateResult.PASS}, failed_name

    assert evaluate_gates(
        {**base, "max_drawdown": Decimal("0.149999")}, policy, kind="development"
    )["max_drawdown_aggregate"] is GateResult.PASS
    assert evaluate_gates(
        {**base, "profit_factor": Decimal("1.20")}, policy, kind="development"
    )["profit_factor_base"] is GateResult.PASS
    assert evaluate_gates(
        {**base, "profit_factor_bootstrap_lower_bound": Decimal("1.000001")},
        policy,
        kind="development",
    )["profit_factor_bootstrap"] is GateResult.PASS
    assert evaluate_gates(
        {**base, "positive_window_fraction": Decimal("0.60")},
        policy,
        kind="development",
    )["positive_window_fraction"] is GateResult.PASS

    oos_base = passing_oos_metrics()
    oos_gates = evaluate_gates(oos_base, policy, kind="oos")
    assert set(oos_gates.values()) == {GateResult.PASS}
    assert "psr" not in oos_gates
    assert "dsr" not in oos_gates
    assert "profit_factor_bootstrap" not in oos_gates
    assert "neighborhood" not in oos_gates
    assert "leave_one_symbol_out" not in oos_gates
    oos_violations = {
        "trades": {"trade_count": 19},
        "total_return": {"total_return": Decimal("0.00")},
        "profit_factor": {"profit_factor": Decimal("1.049999")},
        "max_drawdown": {"max_drawdown": Decimal("0.15")},
        "sealed_window": {"sealed_window_months": 5},
    }
    for failed_name, patch in oos_violations.items():
        result = evaluate_gates({**oos_base, **patch}, policy, kind="oos")
        assert result[failed_name] is GateResult.FAIL, failed_name
        others = {name for name in result if name != failed_name}
        assert {result[name] for name in others} == {GateResult.PASS}, failed_name
    assert evaluate_gates(
        {**oos_base, "total_return": Decimal("0.000001")}, policy, kind="oos"
    )["total_return"] is GateResult.PASS
    assert evaluate_gates(
        {**oos_base, "profit_factor": Decimal("1.05")}, policy, kind="oos"
    )["profit_factor"] is GateResult.PASS


def test_walk_forward_boundary_membership_is_half_open() -> None:
    boundary = datetime(2020, 7, 1, tzinfo=UTC)
    assert in_half_open(boundary, START, boundary) is False
    assert in_half_open(boundary, boundary, END) is True
    curve = (
        EquityPoint(START, Decimal("10000")),
        EquityPoint(datetime(2020, 3, 1, 16, tzinfo=UTC), Decimal("10100")),
        EquityPoint(boundary, Decimal("11000")),
        EquityPoint(END, Decimal("11000")),
    )
    first_window = window_equities(curve, START, boundary, Decimal("10000"))
    second_window = window_equities(curve, boundary, END, Decimal("10000"))
    assert Decimal("11000") not in first_window
    assert Decimal("10100") in first_window
    assert second_window[0] == Decimal("10100")
    assert Decimal("11000") in second_window[1:]
    trades = (
        TradeRecord(
            symbol="BTCUSDT",
            direction="LONG",
            entry_time=datetime(2020, 3, 1, 8, tzinfo=UTC),
            exit_time=datetime(2020, 3, 1, 16, tzinfo=UTC),
            net_pnl=Decimal("100"),
        ),
        TradeRecord(
            symbol="ETHUSDT",
            direction="LONG",
            entry_time=boundary,
            exit_time=boundary,
            net_pnl=Decimal("900"),
        ),
    )
    artifacts = EvaluationArtifacts(
        trades=trades,
        starting_equity=Decimal("10000"),
        start=START,
        end=END,
        equity_curve=curve,
        neighborhood=tuple(_survival() for _ in range(6)),
        leave_one_out=tuple(_survival() for _ in range(4)),
        parameters=StrategyParameters(),
    )
    metrics = compute_metrics(
        artifacts, EvaluationPolicy.v2(), kind="development", trial_count=1
    )
    assert metrics["concentration_period_max"] == Decimal("0.9")
    assert metrics["window_count"] == 2


def test_walk_forward_windows_are_chronological_and_non_overlapping() -> None:
    windows = walk_forward_windows(START, END, months=6)
    assert windows == (
        (START, datetime(2020, 7, 1, tzinfo=UTC)),
        (datetime(2020, 7, 1, tzinfo=UTC), END),
    )
    assert windows[0][1] == windows[1][0]
    assert all(left[0] < left[1] for left in windows)
    short = walk_forward_windows(START, datetime(2020, 3, 1, tzinfo=UTC), months=6)
    assert short == ((START, datetime(2020, 3, 1, tzinfo=UTC)),)


def test_neighborhood_wrong_cardinality_fails_closed() -> None:
    artifacts = replace(
        _artifacts(),
        parameters=StrategyParameters(),
        neighborhood=(_survival(),),
    )
    bundle = evaluate_development(artifacts)
    assert bundle.metrics["neighborhood_required_cases"] == 6
    assert bundle.metrics["neighborhood_case_count"] == 1
    assert bundle.metrics["neighborhood_survival_fraction"] == Decimal("0")
    assert bundle.gates["neighborhood"] is GateResult.FAIL


def test_psr_converts_annualized_benchmark_to_weekly() -> None:
    import math

    weekly_star = annualized_psr_benchmark(0.50, "weekly_utc")
    assert weekly_star == pytest.approx(0.50 / math.sqrt(52))
    returns = [0.01, -0.002, 0.015, 0.0, 0.008, -0.004, 0.012, 0.003]
    converted = probabilistic_sharpe_ratio(returns, weekly_star)
    raw_weekly_units = probabilistic_sharpe_ratio(returns, 0.50)
    assert converted != raw_weekly_units
    assert converted > raw_weekly_units
    assert (
        EvaluationPolicy.v2().document["development"]["statistical_confidence"]["psr"][
            "benchmark_sharpe"
        ]
        == 0.50
    )


def test_neighborhood_perturbs_each_numeric_parameter_ten_percent() -> None:
    params = StrategyParameters()
    neighbors = perturbed_parameters(params)
    assert len(neighbors) == 6
    lookbacks = {item.hourly_ema_lookback for item in neighbors}
    stops = {item.stop_pct for item in neighbors}
    tps = {item.take_profit_r for item in neighbors}
    assert 18 in lookbacks and 22 in lookbacks
    assert Decimal("0.0135") in stops and Decimal("0.0165") in stops
    assert Decimal("1.8") in tps and Decimal("2.2") in tps


def test_evaluate_development_is_deterministic_over_trade_artifacts() -> None:
    trades = []
    for month in (1, 3, 8, 10):
        for i, symbol in enumerate(SUPPORTED_SYMBOLS):
            pnl = "12" if month < 7 else "8"
            trades.append(_trade(symbol, 2 + i, pnl, month=month, fees="0.1"))
    artifacts = replace(
        _artifacts(trades=tuple(trades)),
        equity_curve=(
            EquityPoint(START, Decimal("10000")),
            EquityPoint(datetime(2020, 6, 30, tzinfo=UTC), Decimal("10480")),
            EquityPoint(END, Decimal("10800")),
        ),
        trial_daily_returns=(
            tuple(
                (datetime(2020, 1, 1 + day, tzinfo=UTC), Decimal("0.001"))
                for day in range(5)
            ),
        ),
    )
    first = evaluate_development(artifacts)
    second = evaluate_development(artifacts)
    assert first.bundle_hash == second.bundle_hash
    assert first.kind == "development"
    assert first.policy_hash == EvaluationPolicy.v2().policy_hash
    assert first.metrics["window_count"] == 2
    assert first.gates["max_drawdown_aggregate"] in {GateResult.PASS, GateResult.FAIL}
    assert "profit_factor_bootstrap" in first.gates
    assert first.consumed_budget is None


def test_family_budget_mutation_and_technical_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id = db.create_family()
    artifacts = _artifacts()
    for _ in range(8):
        evaluate_development(artifacts, db=db, family_id=family_id)
    assert db.get_budget(family_id).performance_evaluated_versions == 8
    with pytest.raises(BudgetExhausted):
        evaluate_development(artifacts, db=db, family_id=family_id)
    assert db.get_budget(family_id).performance_evaluated_versions == 8

    other = db.create_family()
    with pytest.raises(TechnicalEvaluationError):
        evaluate_development(
            replace(artifacts, technical_error=True, technical_kind="DATA_BLOCKED"),
            db=db,
            family_id=other,
        )
    assert db.get_budget(other).performance_evaluated_versions == 0

    successor = replace(artifacts, prior_version_id="prev", mutation_count=1)
    bundle = evaluate_development(successor, db=db, family_id=other)
    assert bundle.gates["mutations_per_successor"] is GateResult.PASS
    bad = evaluate_development(
        replace(successor, mutation_count=2), db=db, family_id=other
    )
    assert bad.gates["mutations_per_successor"] is GateResult.FAIL
    assert bad.outcome is GateResult.FAIL


def test_sealed_oos_once_rejection_and_research_prohibition(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    family_id = db.create_family()
    trades = tuple(
        _trade(symbol, 3, "5", month=1) for symbol in SUPPORTED_SYMBOLS for _ in range(6)
    )
    oos_arts = EvaluationArtifacts(
        trades=trades,
        starting_equity=Decimal("10000"),
        start=END,
        end=OOS_END,
        equity_curve=(
            EquityPoint(END, Decimal("10000")),
            EquityPoint(OOS_END, Decimal("10120")),
        ),
        performance_evaluated_versions=1,
    )
    sealed = SealedOos(oos_arts, path="C:/secret/oos.parquet", credentials="token")
    assert "secret" not in repr(sealed)
    assert "token" not in repr(sealed)
    first = evaluate_oos_once(sealed, db=db, family_id=family_id)
    assert first.kind == "oos"
    assert db.get_budget(family_id).oos_evaluations == 1
    assert "profit_factor_bootstrap" not in first.gates
    assert "dsr" not in first.metrics or first.metrics.get("dsr") is None
    if first.outcome is GateResult.FAIL:
        assert first.family_disposition == "REJECTED"
    else:
        assert first.family_disposition == "READY_FOR_PAPER"
    with pytest.raises(SealedOosError):
        evaluate_oos_once(oos_arts, db=db, family_id=family_id)
    assert db.get_budget(family_id).oos_evaluations == 1

    zero = evaluate_oos_once(
        replace(
            oos_arts,
            equity_curve=(
                EquityPoint(END, Decimal("10000")),
                EquityPoint(OOS_END, Decimal("10000")),
            ),
            trades=tuple(replace(row, net_pnl=Decimal("0")) for row in trades),
        ),
        family_id="no-db",
    )
    assert zero.gates["total_return"] is GateResult.FAIL
    assert zero.family_disposition == "REJECTED"
    assert zero.outcome is GateResult.FAIL

    development = evaluate_development(_artifacts())
    with pytest.raises(ValueError, match="OOS"):
        research_inputs(development=development, oos=first)
    with pytest.raises(ValueError, match="OOS"):
        research_inputs({"ledger": {}, "oos_trades": []})
    payload = research_inputs(development=development, ledger={"notes": "ok"})
    assert "oos" not in payload
    assert payload["development"]["kind"] == "development"


def test_golden_result_bundle_hash_is_stable() -> None:
    trades = tuple(
        TradeRecord(
            symbol=SUPPORTED_SYMBOLS[i % 4],
            direction="LONG" if i % 2 == 0 else "SHORT",
            entry_time=datetime(2020, 1 + (i % 12), 2, 8, tzinfo=UTC),
            exit_time=datetime(2020, 1 + (i % 12), 2, 16, tzinfo=UTC),
            net_pnl=Decimal("3.25") if i % 3 else Decimal("-1.50"),
            fees=Decimal("0.04"),
            funding=Decimal("-0.01") if i % 2 else Decimal("0.02"),
            slippage_cost=Decimal("0.01"),
        )
        for i in range(8)
    )
    artifacts = EvaluationArtifacts(
        trades=trades,
        starting_equity=Decimal("10000"),
        start=START,
        end=END,
        equity_curve=(
            EquityPoint(START, Decimal("10000")),
            EquityPoint(datetime(2020, 6, 1, tzinfo=UTC), Decimal("10010")),
            EquityPoint(END, Decimal("10020")),
        ),
        neighborhood=tuple(_survival(ok=i != 5) for i in range(6)),
        leave_one_out=tuple(_survival(ok=i != 3) for i in range(4)),
        trial_daily_returns=(
            tuple(
                (
                    datetime(2020, 1, 1, tzinfo=UTC),
                    Decimal("0.001") if i == 0 else Decimal("-0.0005"),
                )
                for i in range(1)
            ),
        ),
        performance_evaluated_versions=1,
        parameters=StrategyParameters(),
    )
    first = evaluate_development(artifacts)
    second = evaluate_development(artifacts)
    assert isinstance(first, ResultBundle)
    assert first.bundle_hash == second.bundle_hash
    assert first.bundle_hash == GOLDEN_BUNDLE_HASH
    assert len(first.bundle_hash) == 64
