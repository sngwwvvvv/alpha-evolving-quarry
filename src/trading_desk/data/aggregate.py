from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from trading_desk.data.contracts import (
    DAILY_TRANSFORMATION,
    DAY,
    HOUR,
    HOURLY_TRANSFORMATION,
    MINUTE,
    OK,
    TIMEFRAME_1D,
    TIMEFRAME_1H,
    DataResult,
    DerivedBar,
    Kline1m,
    blocked,
    source_hash as snapshot_source_hash,
    transformation_hash,
)
from trading_desk.data.validate import floor_utc, kline_reasons


def _group_by_symbol(klines: Sequence[Kline1m]) -> dict[str, list[Kline1m]]:
    grouped: dict[str, list[Kline1m]] = defaultdict(list)
    for bar in klines:
        grouped[bar.symbol].append(bar)
    return grouped


def _aggregate_group(group: Sequence[Kline1m]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    return (
        group[0].open,
        max(bar.high for bar in group),
        min(bar.low for bar in group),
        group[-1].close,
        sum((bar.volume for bar in group), Decimal("0")),
    )


def _derive_bars(
    klines: Sequence[Kline1m],
    *,
    timeframe: str,
    bucket_size: timedelta,
    minutes_per_bar: int,
    transformation_version: str,
    source_hash: str | None,
) -> DataResult:
    reasons = kline_reasons(klines, require_all_symbols=False)
    if reasons:
        return blocked(*reasons)
    if not klines:
        return blocked("missing bar: empty kline partition")

    if source_hash is None:
        source_hash = snapshot_source_hash(klines, (), ())

    tf_hash = transformation_hash(source_hash, transformation_version)
    derived: list[DerivedBar] = []
    for symbol, bars in sorted(_group_by_symbol(klines).items()):
        ordered = sorted(bars, key=lambda bar: bar.open_time)
        series_start = ordered[0].open_time
        series_end = ordered[-1].open_time + MINUTE
        buckets: dict[datetime, list[Kline1m]] = defaultdict(list)
        for bar in ordered:
            if timeframe == TIMEFRAME_1D:
                bucket = floor_utc(bar.open_time, day=True)
            else:
                bucket = floor_utc(bar.open_time, hour=True)
            buckets[bucket].append(bar)
        for bucket, group in sorted(buckets.items()):
            group.sort(key=lambda bar: bar.open_time)
            bucket_end = bucket + bucket_size
            complete = (
                len(group) == minutes_per_bar
                and group[0].open_time == bucket
                and group[-1].open_time + MINUTE == bucket_end
            )
            if complete:
                open_, high, low, close, volume = _aggregate_group(group)
                derived.append(
                    DerivedBar(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=bucket,
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        source_hash=source_hash,
                        transformation_version=transformation_version,
                        transformation_hash=tf_hash,
                    )
                )
                continue
            if bucket < series_start or bucket_end > series_end:
                continue
            return blocked(f"missing bar: {symbol} {bucket.isoformat()}")

    derived.sort(key=lambda bar: (bar.symbol, bar.open_time))
    return DataResult(status=OK, bars=tuple(derived))


def derive_hourly_bars(
    klines: Sequence[Kline1m],
    *,
    source_hash: str | None = None,
) -> DataResult:
    return _derive_bars(
        klines,
        timeframe=TIMEFRAME_1H,
        bucket_size=HOUR,
        minutes_per_bar=60,
        transformation_version=HOURLY_TRANSFORMATION,
        source_hash=source_hash,
    )


def derive_utc_daily_bars(
    klines: Sequence[Kline1m],
    *,
    source_hash: str | None = None,
) -> DataResult:
    return _derive_bars(
        klines,
        timeframe=TIMEFRAME_1D,
        bucket_size=DAY,
        minutes_per_bar=1440,
        transformation_version=DAILY_TRANSFORMATION,
        source_hash=source_hash,
    )
