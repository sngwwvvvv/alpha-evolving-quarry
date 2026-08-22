from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from trading_desk.config import SUPPORTED_SYMBOLS
from trading_desk.data.aggregate import derive_hourly_bars, derive_utc_daily_bars
from trading_desk.data.contracts import (
    EMA_200_WARMUP_DAYS,
    OK,
    DataResult,
    DataSnapshot,
    Kline1m,
    blocked,
    derived_data_hash,
    macro_hash,
)
from trading_desk.data.validate import first_completed_utc_day, validate_snapshot


def common_evaluation_start(klines: Sequence[Kline1m]) -> DataResult:
    firsts: dict[str, Kline1m] = {}
    present: set[str] = set()
    for bar in klines:
        present.add(bar.symbol)
        previous = firsts.get(bar.symbol)
        if previous is None or bar.open_time < previous.open_time:
            firsts[bar.symbol] = bar
    missing = [symbol for symbol in SUPPORTED_SYMBOLS if symbol not in firsts]
    if missing:
        return blocked(*(f"missing bar: {symbol}" for symbol in missing))
    extra = [symbol for symbol in present if symbol not in SUPPORTED_SYMBOLS]
    if extra:
        return blocked(*(f"unsupported symbol: {symbol}" for symbol in extra))

    common = max(firsts[symbol].open_time for symbol in SUPPORTED_SYMBOLS)
    start = first_completed_utc_day(common)
    return DataResult(status=OK, evaluation_start=start + timedelta(days=EMA_200_WARMUP_DAYS))


def build_data_snapshot(snapshot: DataSnapshot) -> DataResult:
    raw = DataSnapshot(
        klines_1m=snapshot.klines_1m,
        funding=snapshot.funding,
        metadata=snapshot.metadata,
        macro_events=snapshot.macro_events,
    )
    checked = validate_snapshot(raw)
    if checked.status != OK or checked.snapshot is None:
        return checked

    source = checked.snapshot.source_hash
    hourly = derive_hourly_bars(checked.snapshot.klines_1m, source_hash=source)
    if hourly.status != OK:
        return hourly
    daily = derive_utc_daily_bars(checked.snapshot.klines_1m, source_hash=source)
    if daily.status != OK:
        return daily

    start = common_evaluation_start(checked.snapshot.klines_1m)
    evaluation_start = start.evaluation_start if start.status == OK else None
    bars = hourly.bars + daily.bars
    built = DataSnapshot(
        klines_1m=checked.snapshot.klines_1m,
        funding=checked.snapshot.funding,
        metadata=checked.snapshot.metadata,
        macro_events=checked.snapshot.macro_events,
        hourly_bars=hourly.bars,
        daily_bars=daily.bars,
        source_hash=source,
        derived_data_hash=derived_data_hash(bars),
        macro_hash=macro_hash(checked.snapshot.macro_events),
        evaluation_start=evaluation_start,
    )
    return DataResult(status=OK, snapshot=built)
