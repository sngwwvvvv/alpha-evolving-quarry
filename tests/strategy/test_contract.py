from __future__ import annotations

import ast
import inspect
from datetime import datetime
from decimal import Decimal

import pytest

from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.contracts import TIMEFRAME_1H, DerivedBar, transformation_hash
from trading_desk.strategy.default import DEFAULT_FAMILY, DefaultStrategy, ema, regime
from trading_desk.strategy.models import (
    AGGREGATE_PLANNED_RISK,
    DAILY_LOSS_STOP,
    GROSS_LEVERAGE_CEILING,
    LONG,
    MARGIN_MODE,
    MDD_HALT,
    NEUTRAL,
    PER_POSITION_RISK,
    SHORT,
    SYSTEM_LEVERAGE,
    StrategyParameters,
    StrategyVersion,
    make_strategy_version,
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _hour_bar(symbol: str, close: Decimal, open_time: datetime | None = None) -> DerivedBar:
    stamp = open_time or _utc(2020, 7, 19, 19)
    return DerivedBar(
        symbol=symbol,
        timeframe=TIMEFRAME_1H,
        open_time=stamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
        source_hash="s" * 64,
        transformation_version="aggregate-1h-v1",
        transformation_hash=transformation_hash("s" * 64, "aggregate-1h-v1"),
    )


def _increasing(count: int, start: int = 100) -> list[Decimal]:
    return [Decimal(start + index) for index in range(count)]


def _decreasing(count: int, start: int = 300) -> list[Decimal]:
    return [Decimal(start - index) for index in range(count)]


def test_default_family_uses_one_topology_for_every_symbol() -> None:
    family = DEFAULT_FAMILY
    assert family.topology
    assert family.feature_set
    assert family.entry_exit
    assert family.lifecycle
    params = StrategyParameters()
    mapping = params.as_mapping()
    assert "hourly_ema_lookback" in mapping
    assert "stop_pct" in mapping
    assert "take_profit_r" in mapping
    for symbol in SUPPORTED_SYMBOLS:
        assert symbol not in mapping
        assert symbol.lower() not in {key.lower() for key in mapping}


def test_parameters_reject_symbol_lookup_tables() -> None:
    with pytest.raises(ValueError, match="asset-specific"):
        StrategyParameters.from_mapping({"BTCUSDT": {"hourly_ema_lookback": 10}})
    with pytest.raises(ValueError, match="asset-specific"):
        StrategyParameters.from_mapping(
            {"hourly_ema_lookback": 20, "ETHUSDT": 1.5, "stop_pct": "0.015", "take_profit_r": "2"}
        )


def test_default_strategy_source_has_no_symbol_branch() -> None:
    import trading_desk.strategy.default as default_mod

    tree = ast.parse(inspect.getsource(default_mod))
    forbidden = set(SUPPORTED_SYMBOLS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in forbidden:
            raise AssertionError(f"asset-specific constant {node.value!r}")


def test_signal_graph_tp_sl_and_lifecycle_are_identical_across_symbols() -> None:
    strategy = DefaultStrategy()
    dailies = _increasing(200)
    hourlies = _increasing(20)
    close = hourlies[-1]
    published = _utc(2020, 7, 19, 20)
    signals = []
    for symbol in SUPPORTED_SYMBOLS:
        bar = _hour_bar(symbol, close)
        signal = strategy.signal(symbol, bar, hourlies, dailies)
        assert signal is not None
        assert signal.direction == LONG
        assert signal.published_at == published
        signals.append(signal)
    first = signals[0]
    for signal, symbol in zip(signals, SUPPORTED_SYMBOLS):
        assert signal.symbol == symbol
        assert signal.direction == first.direction
        assert signal.close == first.close
        assert signal.stop == first.stop
        assert signal.take_profit == first.take_profit
        assert signal.bar_open_time == first.bar_open_time
        assert signal.published_at == first.published_at
        assert signal.stop < signal.close
        assert signal.take_profit > signal.close


def test_short_signal_uses_the_same_stop_and_target_method() -> None:
    strategy = DefaultStrategy()
    dailies = _decreasing(200)
    hourlies = _decreasing(20)
    signals = [
        strategy.signal(symbol, _hour_bar(symbol, hourlies[-1]), hourlies, dailies)
        for symbol in SUPPORTED_SYMBOLS
    ]
    assert all(signal is not None and signal.direction == SHORT for signal in signals)
    first = signals[0]
    assert first is not None
    for signal in signals[1:]:
        assert signal is not None
        assert signal.stop == first.stop
        assert signal.take_profit == first.take_profit
        assert signal.stop > signal.close
        assert signal.take_profit < signal.close


def test_leverage_and_risk_formula_are_system_invariants() -> None:
    assert MARGIN_MODE == "isolated"
    assert SYSTEM_LEVERAGE == Decimal("2")
    assert GROSS_LEVERAGE_CEILING == Decimal("2")
    assert PER_POSITION_RISK == Decimal("0.005")
    assert AGGREGATE_PLANNED_RISK == Decimal("0.02")
    assert DAILY_LOSS_STOP == Decimal("0.02")
    assert MDD_HALT == Decimal("0.15")
    params = StrategyParameters().as_mapping()
    for forbidden in ("leverage", "fee_rate", "slippage_rate", "per_position_risk"):
        assert forbidden not in params


def test_strategy_version_is_immutable_and_hashed() -> None:
    version = make_strategy_version(code_commit="c" * 40)
    assert isinstance(version, StrategyVersion)
    assert version.family_id == DEFAULT_FAMILY.family_id
    assert len(version.spec_hash) == 64
    assert version.parameters == StrategyParameters()
    with pytest.raises(AttributeError):
        version.parameters = StrategyParameters(hourly_ema_lookback=10)  # type: ignore[misc]
    again = make_strategy_version(code_commit="c" * 40)
    assert again.spec_hash == version.spec_hash


def test_regime_uses_completed_daily_ema50_and_ema200() -> None:
    assert regime(_increasing(200)) == "BULL"
    assert regime(_decreasing(200)) == "BEAR"
    assert regime([Decimal("100")] * 200) == NEUTRAL
    assert regime(_increasing(199)) == NEUTRAL
    ema50 = ema(_increasing(200), 50)
    ema200 = ema(_increasing(200), 200)
    assert ema50 is not None and ema200 is not None
    assert ema50 > ema200


def test_bull_blocks_short_bear_blocks_long_neutral_blocks_all() -> None:
    strategy = DefaultStrategy()
    up_hour = _increasing(20)
    down_hour = _decreasing(20)
    assert strategy.signal("BTCUSDT", _hour_bar("BTCUSDT", down_hour[-1]), down_hour, _increasing(200)) is None
    assert strategy.signal("ETHUSDT", _hour_bar("ETHUSDT", up_hour[-1]), up_hour, _decreasing(200)) is None
    assert strategy.signal("XRPUSDT", _hour_bar("XRPUSDT", up_hour[-1]), up_hour, [Decimal("100")] * 200) is None
    assert strategy.signal("SOLUSDT", _hour_bar("SOLUSDT", down_hour[-1]), down_hour, [Decimal("100")] * 200) is None


def test_signal_ignores_unpublished_future_hourly_closes() -> None:
    strategy = DefaultStrategy()
    dailies = _increasing(200)
    hourlies = _increasing(20)
    future = hourlies + [Decimal("10"), Decimal("10")]
    left = strategy.signal("BTCUSDT", _hour_bar("BTCUSDT", hourlies[-1]), hourlies, dailies)
    right = strategy.signal("BTCUSDT", _hour_bar("BTCUSDT", hourlies[-1]), future[:20], dailies)
    assert left is not None and right is not None
    assert left.direction == right.direction
    assert left.stop == right.stop
    assert left.take_profit == right.take_profit


def test_incomplete_daily_cannot_change_regime() -> None:
    completed = _increasing(200)
    with_incomplete = completed + [Decimal("1")]
    assert regime(completed) == regime(with_incomplete[:200])
    strategy = DefaultStrategy()
    hourlies = _increasing(20)
    using_completed = strategy.signal("BTCUSDT", _hour_bar("BTCUSDT", hourlies[-1]), hourlies, completed)
    using_truncated = strategy.signal("BTCUSDT", _hour_bar("BTCUSDT", hourlies[-1]), hourlies, with_incomplete[:200])
    assert using_completed is not None and using_truncated is not None
    assert using_completed.direction == using_truncated.direction
