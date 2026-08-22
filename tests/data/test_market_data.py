from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.aggregate import derive_hourly_bars, derive_utc_daily_bars
from trading_desk.data.contracts import (
    DATA_BLOCKED,
    EMA_200_WARMUP_DAYS,
    OK,
    ContractMetadata,
    DataSnapshot,
    Funding,
    Kline1m,
    MacroEvent,
)
from trading_desk.data.snapshot import build_data_snapshot, common_evaluation_start
from trading_desk.data.validate import validate_snapshot


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _kline(
    symbol: str,
    open_time: datetime,
    open: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "1",
) -> Kline1m:
    return Kline1m(
        symbol=symbol,
        open_time=open_time,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _minutes(symbol: str, start: datetime, count: int, **kwargs: str) -> list[Kline1m]:
    return [_kline(symbol, start + timedelta(minutes=index), **kwargs) for index in range(count)]


def _four_symbol_minutes(start: datetime, count: int, **kwargs: str) -> list[Kline1m]:
    bars: list[Kline1m] = []
    for symbol in SUPPORTED_SYMBOLS:
        bars.extend(_minutes(symbol, start, count, **kwargs))
    return bars


def _metadata(symbol: str, effective_from: datetime) -> ContractMetadata:
    return ContractMetadata(
        symbol=symbol,
        effective_from=effective_from,
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        listing_state="TRADING",
    )


def _four_metadata(effective_from: datetime) -> tuple[ContractMetadata, ...]:
    return tuple(_metadata(symbol, effective_from) for symbol in SUPPORTED_SYMBOLS)


def _funding(symbol: str, funding_time: datetime, rate: str = "0.0001") -> Funding:
    return Funding(symbol=symbol, funding_time=funding_time, funding_rate=Decimal(rate))


def _fundings_for(klines: list[Kline1m]) -> list[Funding]:
    by_symbol: dict[str, list[Kline1m]] = {}
    for bar in klines:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    rows: list[Funding] = []
    interval = timedelta(hours=8)
    for symbol, bars in by_symbol.items():
        first = min(bar.open_time for bar in bars)
        end = max(bar.open_time for bar in bars) + timedelta(minutes=1)
        slot = first.replace(hour=(first.hour // 8) * 8, minute=0, second=0, microsecond=0)
        while slot < end:
            rows.append(_funding(symbol, slot))
            slot += interval
    return rows


def _macro(publication_time: datetime) -> MacroEvent:
    return MacroEvent(
        source="free-test",
        event_id="CPI",
        event_time=_utc(2020, 1, 1, 13, 30),
        publication_time=publication_time,
        actual=Decimal("2.4"),
        unit="percent",
        vintage="2020-01",
    )


def _snapshot(
    klines: list[Kline1m],
    *,
    funding: list[Funding] | None = None,
    metadata: tuple[ContractMetadata, ...] | None = None,
    macro_events: tuple[MacroEvent, ...] = (),
) -> DataSnapshot:
    first = min(bar.open_time for bar in klines)
    return DataSnapshot(
        klines_1m=tuple(klines),
        funding=tuple(_fundings_for(klines) if funding is None else funding),
        metadata=_four_metadata(first) if metadata is None else metadata,
        macro_events=macro_events,
    )


def _distinct_hour(symbol: str, start: datetime) -> list[Kline1m]:
    bars: list[Kline1m] = []
    for index in range(60):
        open_ = Decimal(100 + index)
        close = Decimal(101 + index)
        high = Decimal(110 + index)
        bars.append(
            Kline1m(
                symbol=symbol,
                open_time=start + timedelta(minutes=index),
                open=open_,
                high=high,
                low=Decimal("90"),
                close=close,
                volume=Decimal("1"),
            )
        )
    return bars


START = _utc(2020, 1, 1)


def test_duplicate_klines_are_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 3)
    klines.append(klines[0])
    result = validate_snapshot(_snapshot(klines))
    assert result.status == DATA_BLOCKED
    assert any("duplicate" in reason for reason in result.reasons)
    assert result.snapshot is None


def test_reversed_timestamps_are_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 3)
    btc = [bar for bar in klines if bar.symbol == "BTCUSDT"]
    others = [bar for bar in klines if bar.symbol != "BTCUSDT"]
    reversed_btc = [btc[0], btc[2], btc[1]]
    result = validate_snapshot(_snapshot(reversed_btc + others))
    assert result.status == DATA_BLOCKED
    assert any("reversed" in reason or "non-monotonic" in reason for reason in result.reasons)


def test_invalid_ohlc_and_negative_volume_are_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 2)
    broken = [
        replace(bar, high=Decimal("80")) if bar.symbol == "ETHUSDT" and bar.open_time == START else bar
        for bar in klines
    ]
    ohlc = validate_snapshot(_snapshot(broken))
    assert ohlc.status == DATA_BLOCKED
    assert any("OHLC" in reason for reason in ohlc.reasons)

    negative = [
        replace(bar, volume=Decimal("-1")) if bar.symbol == "XRPUSDT" and bar.open_time == START else bar
        for bar in klines
    ]
    volume = validate_snapshot(_snapshot(negative))
    assert volume.status == DATA_BLOCKED
    assert any("volume" in reason for reason in volume.reasons)


def test_missing_bar_is_data_blocked_and_not_interpolated() -> None:
    klines = _four_symbol_minutes(START, 60)
    gapped = [
        bar
        for bar in klines
        if not (bar.symbol == "BTCUSDT" and bar.open_time == START + timedelta(minutes=10))
    ]
    result = validate_snapshot(_snapshot(gapped))
    assert result.status == DATA_BLOCKED
    assert any("missing bar" in reason for reason in result.reasons)
    assert result.snapshot is None

    derived = derive_hourly_bars(tuple(gapped))
    assert derived.status == DATA_BLOCKED
    assert derived.bars == ()


def test_missing_funding_is_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 3)
    result = validate_snapshot(_snapshot(klines, funding=[]))
    assert result.status == DATA_BLOCKED
    assert any("funding" in reason for reason in result.reasons)


def test_metadata_gap_is_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 3)
    late = _four_metadata(START + timedelta(minutes=1))
    result = validate_snapshot(_snapshot(klines, metadata=late))
    assert result.status == DATA_BLOCKED
    assert any("metadata gap" in reason for reason in result.reasons)

    incomplete = tuple(
        replace(row, min_notional=Decimal("0")) if row.symbol == "SOLUSDT" else row
        for row in _four_metadata(START)
    )
    missing_size = validate_snapshot(_snapshot(klines, metadata=incomplete))
    assert missing_size.status == DATA_BLOCKED
    assert any("metadata gap" in reason for reason in missing_size.reasons)


def test_valid_four_symbol_snapshot_records_source_and_transformation_hashes() -> None:
    klines = _four_symbol_minutes(START, 180)
    result = build_data_snapshot(_snapshot(klines, macro_events=(_macro(_utc(2020, 1, 1, 13, 30)),)))
    assert result.status == OK
    assert result.snapshot is not None
    snapshot = result.snapshot
    assert snapshot.source_hash
    assert snapshot.derived_data_hash
    assert len(snapshot.source_hash) == 64
    assert len(snapshot.derived_data_hash) == 64
    assert len(snapshot.hourly_bars) == 12
    assert snapshot.daily_bars == ()
    assert {bar.timeframe for bar in snapshot.hourly_bars} == {"1h"}
    assert all(bar.source_hash == snapshot.source_hash for bar in snapshot.hourly_bars)
    assert all(bar.transformation_hash for bar in snapshot.hourly_bars)
    again = build_data_snapshot(_snapshot(klines, macro_events=(_macro(_utc(2020, 1, 1, 13, 30)),)))
    assert again.snapshot is not None
    assert again.snapshot.source_hash == snapshot.source_hash
    assert again.snapshot.derived_data_hash == snapshot.derived_data_hash


def test_macro_events_are_stored_but_do_not_change_derived_bars() -> None:
    klines = _four_symbol_minutes(START, 60)
    without_macro = build_data_snapshot(_snapshot(klines))
    with_macro = build_data_snapshot(_snapshot(klines, macro_events=(_macro(_utc(2020, 1, 1, 15, 0)),)))
    assert without_macro.status == OK
    assert with_macro.status == OK
    assert without_macro.snapshot is not None
    assert with_macro.snapshot is not None
    assert with_macro.snapshot.macro_events
    assert without_macro.snapshot.hourly_bars == with_macro.snapshot.hourly_bars
    assert without_macro.snapshot.source_hash == with_macro.snapshot.source_hash
    assert without_macro.snapshot.derived_data_hash == with_macro.snapshot.derived_data_hash
    assert without_macro.snapshot.macro_hash != with_macro.snapshot.macro_hash


def test_derive_hourly_bars_uses_completed_minutes_only() -> None:
    complete = _distinct_hour("BTCUSDT", START)
    trailing = _minutes("BTCUSDT", START + timedelta(hours=1), 15)
    result = derive_hourly_bars(tuple(complete + trailing))
    assert result.status == OK
    assert len(result.bars) == 1
    bar = result.bars[0]
    assert bar.symbol == "BTCUSDT"
    assert bar.timeframe == "1h"
    assert bar.open_time == START
    assert bar.open == Decimal("100")
    assert bar.close == Decimal("160")
    assert bar.high == Decimal("169")
    assert bar.low == Decimal("90")
    assert bar.volume == Decimal("60")


def test_derive_utc_daily_bars_uses_completed_minutes_only() -> None:
    complete = _minutes("ETHUSDT", START, 1440)
    extra = _minutes("ETHUSDT", START + timedelta(days=1), 60)
    result = derive_utc_daily_bars(tuple(complete + extra))
    assert result.status == OK
    assert len(result.bars) == 1
    bar = result.bars[0]
    assert bar.symbol == "ETHUSDT"
    assert bar.timeframe == "1d"
    assert bar.open_time == START
    assert bar.open == Decimal("100")
    assert bar.close == Decimal("105")
    assert bar.high == Decimal("110")
    assert bar.low == Decimal("90")
    assert bar.volume == Decimal("1440")


def test_mismatched_source_hash_is_data_blocked() -> None:
    klines = _four_symbol_minutes(START, 60)
    built = build_data_snapshot(_snapshot(klines))
    assert built.snapshot is not None
    tampered = replace(built.snapshot, source_hash="0" * 64)
    result = validate_snapshot(tampered)
    assert result.status == DATA_BLOCKED
    assert any("source hash" in reason for reason in result.reasons)


def test_common_evaluation_start_is_latest_four_symbol_timestamp_plus_ema200() -> None:
    starts = {
        "BTCUSDT": _utc(2019, 12, 1),
        "ETHUSDT": _utc(2019, 12, 15),
        "XRPUSDT": _utc(2020, 1, 1),
        "SOLUSDT": _utc(2020, 1, 10),
    }
    klines: list[Kline1m] = []
    for symbol, start in starts.items():
        klines.extend(_minutes(symbol, start, 3))
    result = common_evaluation_start(tuple(klines))
    assert result.status == OK
    assert result.evaluation_start == starts["SOLUSDT"] + timedelta(days=EMA_200_WARMUP_DAYS)
    assert result.evaluation_start == _utc(2020, 7, 28)

    mid_day = []
    for symbol, start in starts.items():
        first = start if symbol != "SOLUSDT" else _utc(2020, 1, 10, 12, 0)
        mid_day.extend(_minutes(symbol, first, 3))
    aligned = common_evaluation_start(tuple(mid_day))
    assert aligned.status == OK
    assert aligned.evaluation_start == _utc(2020, 7, 29)


def test_common_evaluation_start_blocks_missing_symbol() -> None:
    klines = (
        _minutes("BTCUSDT", START, 3)
        + _minutes("ETHUSDT", START, 3)
        + _minutes("XRPUSDT", START, 3)
    )
    result = common_evaluation_start(tuple(klines))
    assert result.status == DATA_BLOCKED
    assert any("missing bar: SOLUSDT" in reason for reason in result.reasons)


def test_build_data_snapshot_blocks_when_evaluation_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # validate_snapshot already requires all four symbols, so this branch is
    # defensive; monkeypatch forces the fail-closed path in build_data_snapshot.
    from trading_desk.data import snapshot as snapshot_mod
    from trading_desk.data.contracts import blocked

    monkeypatch.setattr(
        snapshot_mod,
        "common_evaluation_start",
        lambda _klines: blocked("missing bar: SOLUSDT"),
    )
    result = snapshot_mod.build_data_snapshot(_snapshot(_four_symbol_minutes(START, 60)))
    assert result.status == DATA_BLOCKED
    assert result.snapshot is None
    assert any("missing bar: SOLUSDT" in reason for reason in result.reasons)
