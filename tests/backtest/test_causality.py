from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from trading_desk.backtest.account import Account, size_order
from trading_desk.backtest.execution import BacktestEngine, PaperLifecycle
from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.contracts import (
    DAILY_TRANSFORMATION,
    DATA_BLOCKED,
    DAY,
    EMA_200_WARMUP_DAYS,
    HOUR,
    MINUTE,
    TIMEFRAME_1D,
    ContractMetadata,
    DataSnapshot,
    DerivedBar,
    Funding,
    Kline1m,
    MacroEvent,
    source_hash,
    transformation_hash,
)
from trading_desk.strategy.default import DefaultStrategy
from trading_desk.strategy.models import (
    AGGREGATE_PLANNED_RISK,
    GROSS_LEVERAGE_CEILING,
    LONG,
    MDD_FAIL,
    MDD_HALT,
    OK,
    PER_POSITION_RISK,
    SYSTEM_LEVERAGE,
    ExecutionPolicy,
    StrategyParameters,
)

EVAL_START = datetime(2020, 7, 19, tzinfo=UTC)
DAILY_START = EVAL_START - timedelta(days=EMA_200_WARMUP_DAYS)
STARTING_EQUITY = Decimal("10000")
POLICY = ExecutionPolicy(fee_rate=Decimal("0.0004"), slippage_rate=Decimal("0.0005"))


def _kline(
    symbol: str,
    open_time: datetime,
    price: Decimal,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> Kline1m:
    high_px = high if high is not None else price
    low_px = low if low is not None else price
    return Kline1m(
        symbol=symbol,
        open_time=open_time,
        open=price,
        high=high_px,
        low=low_px,
        close=price,
        volume=Decimal("1"),
    )


def _metadata(
    symbol: str,
    *,
    quantity_step: str = "0.001",
    min_quantity: str = "0.001",
    min_notional: str = "5",
    price_tick: str = "0.01",
) -> ContractMetadata:
    return ContractMetadata(
        symbol=symbol,
        effective_from=EVAL_START,
        price_tick=Decimal(price_tick),
        quantity_step=Decimal(quantity_step),
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
        listing_state="TRADING",
    )


def _fundings(klines: list[Kline1m], rate: Decimal = Decimal("0.0001")) -> list[Funding]:
    by_symbol: dict[str, list[Kline1m]] = {}
    for bar in klines:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    rows: list[Funding] = []
    for symbol, bars in by_symbol.items():
        first = min(bar.open_time for bar in bars)
        end = max(bar.open_time for bar in bars) + MINUTE
        slot = first.replace(hour=(first.hour // 8) * 8, minute=0, second=0, microsecond=0)
        while slot < end:
            rows.append(Funding(symbol=symbol, funding_time=slot, funding_rate=rate))
            slot += timedelta(hours=8)
    return rows


def _dailies(symbol: str, source: str, closes: list[Decimal]) -> list[DerivedBar]:
    tf_hash = transformation_hash(source, DAILY_TRANSFORMATION)
    bars: list[DerivedBar] = []
    for index, close in enumerate(closes):
        bars.append(
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
    return bars


def _hour_prices(count: int, *, start: int = 100, slope: int = 1) -> list[Decimal]:
    return [Decimal(start + slope * index) for index in range(count)]


def _minutes_from_hours(
    symbol: str,
    prices: list[Decimal],
    *,
    overrides: dict[datetime, tuple[Decimal, Decimal, Decimal]] | None = None,
) -> list[Kline1m]:
    overrides = overrides or {}
    bars: list[Kline1m] = []
    for index, price in enumerate(prices):
        hour = EVAL_START + timedelta(hours=index)
        for minute in range(60):
            stamp = hour + timedelta(minutes=minute)
            if stamp in overrides:
                open_px, high, low = overrides[stamp]
                bars.append(_kline(symbol, stamp, open_px, high=high, low=low))
            else:
                bars.append(_kline(symbol, stamp, price))
    return bars


def _snapshot(
    hourly_prices: dict[str, list[Decimal]],
    *,
    daily_closes: dict[str, list[Decimal]] | None = None,
    overrides: dict[tuple[str, datetime], tuple[Decimal, Decimal, Decimal]] | None = None,
    metadata: tuple[ContractMetadata, ...] | None = None,
    extra_funding: list[Funding] | None = None,
    evaluation_start: datetime | None = EVAL_START,
    hourly_count: int | None = None,
    funding_rate: Decimal = Decimal("0.0001"),
    macro_events: tuple[MacroEvent, ...] = (),
) -> DataSnapshot:
    count = hourly_count or max(len(prices) for prices in hourly_prices.values())
    klines: list[Kline1m] = []
    for symbol in SUPPORTED_SYMBOLS:
        prices = hourly_prices.get(symbol, [Decimal("100")] * count)
        if len(prices) < count:
            prices = prices + [prices[-1]] * (count - len(prices))
        symbol_overrides = {
            stamp: values for (sym, stamp), values in (overrides or {}).items() if sym == symbol
        }
        klines.extend(_minutes_from_hours(symbol, prices[:count], overrides=symbol_overrides))
    funding = _fundings(klines, funding_rate)
    if extra_funding:
        funding.extend(extra_funding)
    meta = metadata or tuple(_metadata(symbol) for symbol in SUPPORTED_SYMBOLS)
    source = source_hash(klines, funding, meta)
    bull = [Decimal(100 + index) for index in range(EMA_200_WARMUP_DAYS)]
    daily_bars: list[DerivedBar] = []
    series = daily_closes or {symbol: bull for symbol in SUPPORTED_SYMBOLS}
    for symbol in SUPPORTED_SYMBOLS:
        daily_bars.extend(_dailies(symbol, source, series[symbol]))
    return DataSnapshot(
        klines_1m=tuple(klines),
        funding=tuple(funding),
        metadata=meta,
        macro_events=macro_events,
        daily_bars=tuple(daily_bars),
        source_hash=source,
        evaluation_start=evaluation_start,
    )


def _run(
    snapshot: DataSnapshot,
    *,
    parameters: StrategyParameters | None = None,
    policy: ExecutionPolicy = POLICY,
    starting_equity: Decimal = STARTING_EQUITY,
):
    engine = BacktestEngine()
    return engine.run(
        snapshot,
        DefaultStrategy(parameters or StrategyParameters()),
        policy,
        starting_equity,
    )


def test_four_asset_evaluation_requires_start_and_two_hundred_dailies() -> None:
    prices = {symbol: _hour_prices(24) for symbol in SUPPORTED_SYMBOLS}
    missing_start = _snapshot(prices, evaluation_start=None)
    blocked = _run(missing_start)
    assert blocked.status == DATA_BLOCKED
    assert any("evaluation_start" in reason for reason in blocked.reasons)

    short = _snapshot(prices)
    short = DataSnapshot(
        klines_1m=short.klines_1m,
        funding=short.funding,
        metadata=short.metadata,
        daily_bars=tuple(bar for bar in short.daily_bars if bar.open_time < DAILY_START + timedelta(days=199)),
        source_hash=short.source_hash,
        evaluation_start=EVAL_START,
    )
    blocked_days = _run(short)
    assert blocked_days.status == DATA_BLOCKED
    assert any("daily" in reason for reason in blocked_days.reasons)


def test_completed_hour_signal_fills_on_next_hour_open() -> None:
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    result = _run(_snapshot(prices))
    assert result.status == OK
    assert result.fills
    entry = next(fill for fill in result.fills if fill.reason == "entry")
    signal_hour = EVAL_START + timedelta(hours=19)
    assert entry.time == signal_hour + HOUR
    assert entry.time >= signal_hour + HOUR
    hour19_close = prices["BTCUSDT"][19]
    assert entry.price != hour19_close or POLICY.slippage_rate == 0
    assert entry.price > Decimal("119")


def test_identical_series_produce_identical_non_symbol_trade_fields() -> None:
    prices = {symbol: _hour_prices(24) for symbol in SUPPORTED_SYMBOLS}
    result = _run(_snapshot(prices))
    entries = [fill for fill in result.fills if fill.reason == "entry"]
    assert {fill.symbol for fill in entries} == set(SUPPORTED_SYMBOLS)
    first = entries[0]
    for fill in entries[1:]:
        assert fill.quantity == first.quantity
        assert fill.price == first.price
        assert fill.fee == first.fee
        assert fill.time == first.time


def test_regime_uses_only_completed_daily_bars() -> None:
    bull = [Decimal(100 + index) for index in range(EMA_200_WARMUP_DAYS)]
    # Incomplete current-day collapse would flip EMA if used; it must not block the long.
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    overrides = {
        ("BTCUSDT", EVAL_START + timedelta(hours=10, minutes=30)): (Decimal("1"), Decimal("1"), Decimal("1"))
    }
    result = _run(_snapshot(prices, daily_closes={symbol: bull for symbol in SUPPORTED_SYMBOLS}, overrides=overrides))
    assert any(fill.reason == "entry" and fill.symbol == "BTCUSDT" for fill in result.fills)


def test_funding_applies_only_on_required_slots_and_longs_pay_positive_rate() -> None:
    btc = _hour_prices(20) + [Decimal("119")] * 12
    flat = [Decimal("100")] * 32
    prices = {symbol: btc if symbol == "BTCUSDT" else flat for symbol in SUPPORTED_SYMBOLS}
    extra = [
        Funding(symbol="BTCUSDT", funding_time=EVAL_START + timedelta(hours=21, minutes=17), funding_rate=Decimal("0.5"))
    ]
    params = StrategyParameters(take_profit_r=Decimal("50"))
    baseline = _run(_snapshot(prices, hourly_count=32, funding_rate=Decimal("0")), parameters=params)
    paid = _run(_snapshot(prices, extra_funding=extra, hourly_count=32, funding_rate=Decimal("0.01")), parameters=params)
    assert baseline.status == OK and paid.status == OK
    assert paid.ending_equity < baseline.ending_equity
    assert paid.funding_pnl < 0


def test_shorts_receive_positive_funding() -> None:
    eth = _hour_prices(20, start=200, slope=-1) + [Decimal("181")] * 12
    flat = [Decimal("100")] * 32
    prices = {symbol: eth if symbol == "ETHUSDT" else flat for symbol in SUPPORTED_SYMBOLS}
    bear = [Decimal(300 - index) for index in range(EMA_200_WARMUP_DAYS)]
    dailies = {symbol: bear for symbol in SUPPORTED_SYMBOLS}
    params = StrategyParameters(take_profit_r=Decimal("50"))
    zero = _run(_snapshot(prices, daily_closes=dailies, hourly_count=32, funding_rate=Decimal("0")), parameters=params)
    received = _run(
        _snapshot(prices, daily_closes=dailies, hourly_count=32, funding_rate=Decimal("0.01")),
        parameters=params,
    )
    assert received.ending_equity > zero.ending_equity
    assert received.funding_pnl > 0


def test_quantity_is_rounded_to_step_and_tiny_notional_is_rejected() -> None:
    prices = {symbol: _hour_prices(24) for symbol in SUPPORTED_SYMBOLS}
    step_meta = tuple(_metadata(symbol, quantity_step="0.1", min_quantity="0.1") for symbol in SUPPORTED_SYMBOLS)
    stepped = _run(_snapshot(prices, metadata=step_meta))
    entries = [fill for fill in stepped.fills if fill.reason == "entry"]
    assert entries
    for fill in entries:
        assert fill.quantity == fill.quantity.quantize(Decimal("0.1"))
        assert fill.quantity % Decimal("0.1") == 0

    huge = tuple(_metadata(symbol, min_notional="1000000") for symbol in SUPPORTED_SYMBOLS)
    rejected = _run(_snapshot(prices, metadata=huge))
    assert rejected.fills == ()
    assert rejected.rejected
    assert all("notional" in row.reason or "quantity" in row.reason for row in rejected.rejected)


def test_fees_and_slippage_are_adverse() -> None:
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    free = ExecutionPolicy(fee_rate=Decimal("0"), slippage_rate=Decimal("0"))
    costly = ExecutionPolicy(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.002"))
    cheap = _run(_snapshot(prices), policy=free)
    expensive = _run(_snapshot(prices), policy=costly)
    cheap_entry = next(fill for fill in cheap.fills if fill.reason == "entry")
    expensive_entry = next(fill for fill in expensive.fills if fill.reason == "entry")
    assert expensive_entry.price > cheap_entry.price
    assert expensive_entry.fee > cheap_entry.fee
    assert expensive.ending_equity < cheap.ending_equity


def test_same_minute_tp_and_sl_uses_stop_first() -> None:
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    fill_hour = EVAL_START + timedelta(hours=20)
    hit = fill_hour + timedelta(minutes=10)
    overrides = {("BTCUSDT", hit): (Decimal("120"), Decimal("200"), Decimal("50"))}
    result = _run(_snapshot(prices, overrides=overrides, hourly_count=22))
    trade = next(row for row in result.trades if row.symbol == "BTCUSDT")
    assert trade.exit_reason == "stop"
    assert trade.exit_time == hit


def test_size_order_caps_per_position_and_aggregate_planned_risk() -> None:
    meta = _metadata("BTCUSDT")
    sized = size_order(
        equity=STARTING_EQUITY,
        direction=LONG,
        reference_price=Decimal("100"),
        parameters=StrategyParameters(),
        policy=POLICY,
        metadata=meta,
        open_planned_risk=Decimal("0"),
        open_notional=Decimal("0"),
    )
    assert sized is not None
    assert sized.planned_risk <= STARTING_EQUITY * PER_POSITION_RISK
    assert sized.margin * SYSTEM_LEVERAGE == sized.notional
    blocked = size_order(
        equity=STARTING_EQUITY,
        direction=LONG,
        reference_price=Decimal("100"),
        parameters=StrategyParameters(),
        policy=POLICY,
        metadata=meta,
        open_planned_risk=STARTING_EQUITY * AGGREGATE_PLANNED_RISK,
        open_notional=Decimal("0"),
    )
    assert blocked is None


def test_engine_respects_half_percent_and_two_percent_risk() -> None:
    prices = {symbol: _hour_prices(24) for symbol in SUPPORTED_SYMBOLS}
    result = _run(_snapshot(prices))
    entries = [fill for fill in result.fills if fill.reason == "entry"]
    assert len(entries) == 4
    positions_risk = Decimal("0")
    for fill in entries:
        trade_like = next((row for row in result.trades if row.symbol == fill.symbol), None)
        planned = trade_like.planned_risk if trade_like is not None else fill.planned_risk
        assert planned <= STARTING_EQUITY * PER_POSITION_RISK
        positions_risk += planned
    assert positions_risk <= STARTING_EQUITY * AGGREGATE_PLANNED_RISK


def test_gross_leverage_ceiling_and_isolated_two_x() -> None:
    tight = StrategyParameters(stop_pct=Decimal("0.002"), take_profit_r=Decimal("2"))
    prices = {symbol: _hour_prices(24) for symbol in SUPPORTED_SYMBOLS}
    result = _run(_snapshot(prices), parameters=tight)
    entries = [fill for fill in result.fills if fill.reason == "entry"]
    assert entries
    first_time = min(fill.time for fill in entries)
    simultaneous = [fill for fill in entries if fill.time == first_time]
    gross = sum((fill.price * fill.quantity for fill in simultaneous), Decimal("0"))
    assert gross / STARTING_EQUITY <= GROSS_LEVERAGE_CEILING
    for fill in entries:
        assert fill.notional / fill.margin == SYSTEM_LEVERAGE


def test_daily_loss_stop_flattens_and_resumes_next_utc_day() -> None:
    prices = {symbol: _hour_prices(36) if symbol == "BTCUSDT" else [Decimal("100")] * 36 for symbol in SUPPORTED_SYMBOLS}
    fill_hour = EVAL_START + timedelta(hours=20)
    gap = fill_hour + timedelta(minutes=3)
    overrides = {("BTCUSDT", gap): (Decimal("80"), Decimal("80"), Decimal("80"))}
    result = _run(_snapshot(prices, overrides=overrides, hourly_count=36))
    assert any(fill.reason == "daily_stop" for fill in result.fills)
    later_entries = [fill for fill in result.fills if fill.reason == "entry" and fill.time >= EVAL_START + DAY]
    assert later_entries


def test_exact_fifteen_percent_mdd_fails() -> None:
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    fill_hour = EVAL_START + timedelta(hours=20)
    gap = fill_hour + timedelta(minutes=2)
    overrides = {("BTCUSDT", gap): (Decimal("50"), Decimal("50"), Decimal("50"))}
    result = _run(_snapshot(prices, overrides=overrides))
    assert result.status == MDD_FAIL
    assert result.halted
    assert result.max_drawdown >= MDD_HALT
    assert not any(fill.reason == "entry" and fill.time > gap for fill in result.fills)


def test_account_treats_exact_mdd_as_halt() -> None:
    account = Account(STARTING_EQUITY)
    assert account.drawdown(Decimal("8500.01")) < MDD_HALT
    assert not account.should_halt(Decimal("8500.01"))
    assert account.drawdown(Decimal("8500")) == MDD_HALT
    assert account.should_halt(Decimal("8500"))


def test_paper_lifecycle_is_shared_with_backtest_engine() -> None:
    engine = BacktestEngine()
    assert isinstance(engine, PaperLifecycle)
    assert callable(PaperLifecycle.on_minute)


def test_macro_events_do_not_change_fills() -> None:
    prices = {symbol: _hour_prices(24) if symbol == "BTCUSDT" else [Decimal("100")] * 24 for symbol in SUPPORTED_SYMBOLS}
    macro = MacroEvent(
        source="free-test",
        event_id="CPI",
        event_time=EVAL_START,
        publication_time=EVAL_START,
        actual=Decimal("9.9"),
        unit="percent",
        vintage="2020-07",
    )
    plain = _run(_snapshot(prices))
    tagged = _run(_snapshot(prices, macro_events=(macro,)))
    assert [(fill.time, fill.price, fill.quantity, fill.reason) for fill in plain.fills] == [
        (fill.time, fill.price, fill.quantity, fill.reason) for fill in tagged.fills
    ]
