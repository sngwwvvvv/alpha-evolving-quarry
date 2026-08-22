from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Sequence

from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.contracts import (
    DAY,
    FUNDING_INTERVAL,
    MINUTE,
    OK,
    ContractMetadata,
    DataResult,
    DataSnapshot,
    Funding,
    Kline1m,
    MacroEvent,
    blocked,
    canonical_datetime,
    derived_data_hash,
    macro_hash,
    source_hash,
    transformation_hash,
)


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _is_minute_aligned(value: datetime) -> bool:
    return value.second == 0 and value.microsecond == 0


def _ohlc_valid(bar: Kline1m) -> bool:
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return False
    return bar.high >= bar.low and bar.high >= bar.open and bar.high >= bar.close and bar.low <= bar.open and bar.low <= bar.close


def kline_reasons(klines: Sequence[Kline1m], *, require_all_symbols: bool) -> list[str]:
    reasons: list[str] = []
    if not klines:
        reasons.append("missing bar: empty kline partition")
        return reasons

    by_symbol: dict[str, list[Kline1m]] = defaultdict(list)
    for bar in klines:
        if bar.symbol not in SUPPORTED_SYMBOLS:
            reasons.append(f"unsupported symbol: {bar.symbol}")
            continue
        if not _is_utc(bar.open_time) or not _is_minute_aligned(bar.open_time):
            reasons.append(f"non-monotonic timestamps: {bar.symbol}")
            continue
        if not _ohlc_valid(bar):
            reasons.append(f"invalid OHLC: {bar.symbol} {canonical_datetime(bar.open_time)}")
        if bar.volume < 0:
            reasons.append(f"negative volume: {bar.symbol} {canonical_datetime(bar.open_time)}")
        by_symbol[bar.symbol].append(bar)

    if require_all_symbols:
        missing = [symbol for symbol in SUPPORTED_SYMBOLS if symbol not in by_symbol]
        for symbol in missing:
            reasons.append(f"missing bar: {symbol}")

    for symbol, bars in by_symbol.items():
        seen: dict[datetime, int] = defaultdict(int)
        previous: datetime | None = None
        reversed_reported = False
        for bar in bars:
            seen[bar.open_time] += 1
            if previous is not None and bar.open_time < previous and not reversed_reported:
                reasons.append(f"reversed timestamps: {symbol}")
                reversed_reported = True
            previous = bar.open_time
        for open_time, count in seen.items():
            if count > 1:
                reasons.append(f"duplicate kline: {symbol} {canonical_datetime(open_time)}")
        times = sorted(seen)
        for left, right in zip(times, times[1:]):
            expected = left + MINUTE
            if right != expected:
                reasons.append(f"missing bar: {symbol} {canonical_datetime(expected)}")
                break
    return reasons


def _expected_funding_times(bars: Sequence[Kline1m]) -> list[datetime]:
    first = min(bar.open_time for bar in bars)
    end = max(bar.open_time for bar in bars) + MINUTE
    slot = first.replace(hour=(first.hour // 8) * 8, minute=0, second=0, microsecond=0)
    times: list[datetime] = []
    while slot < end:
        times.append(slot)
        slot += FUNDING_INTERVAL
    return times


def funding_reasons(klines: Sequence[Kline1m], funding: Sequence[Funding]) -> list[str]:
    reasons: list[str] = []
    bars_by_symbol: dict[str, list[Kline1m]] = defaultdict(list)
    for bar in klines:
        bars_by_symbol[bar.symbol].append(bar)
    rows_by_symbol: dict[str, list[Funding]] = defaultdict(list)
    for row in funding:
        if row.symbol not in SUPPORTED_SYMBOLS:
            reasons.append(f"unsupported symbol: {row.symbol}")
            continue
        if not _is_utc(row.funding_time):
            reasons.append(f"missing funding: {row.symbol} {row.funding_time!r}")
            continue
        rows_by_symbol[row.symbol].append(row)

    for symbol, bars in bars_by_symbol.items():
        expected = _expected_funding_times(bars)
        seen: dict[datetime, int] = defaultdict(int)
        for row in rows_by_symbol.get(symbol, ()):
            seen[row.funding_time] += 1
        for funding_time, count in seen.items():
            if count > 1:
                reasons.append(f"duplicate funding: {symbol} {canonical_datetime(funding_time)}")
        expected_set = set(expected)
        actual_set = set(seen)
        for missing in sorted(expected_set - actual_set):
            reasons.append(f"missing funding: {symbol} {canonical_datetime(missing)}")
    return reasons


def _size_knowable(row: ContractMetadata) -> bool:
    return min(row.price_tick, row.quantity_step, row.min_quantity, row.min_notional) > 0


def metadata_reasons(klines: Sequence[Kline1m], metadata: Sequence[ContractMetadata]) -> list[str]:
    reasons: list[str] = []
    rows_by_symbol: dict[str, list[ContractMetadata]] = defaultdict(list)
    for row in metadata:
        if row.symbol not in SUPPORTED_SYMBOLS:
            reasons.append(f"unsupported symbol: {row.symbol}")
            continue
        if not _is_utc(row.effective_from):
            reasons.append(f"metadata gap: {row.symbol}")
            continue
        rows_by_symbol[row.symbol].append(row)

    for symbol, rows in rows_by_symbol.items():
        rows.sort(key=lambda row: row.effective_from)
        seen: dict[datetime, int] = defaultdict(int)
        for row in rows:
            seen[row.effective_from] += 1
        for effective_from, count in seen.items():
            if count > 1:
                reasons.append(f"duplicate metadata: {symbol} {canonical_datetime(effective_from)}")

    bars_by_symbol: dict[str, list[Kline1m]] = defaultdict(list)
    for bar in klines:
        bars_by_symbol[bar.symbol].append(bar)

    for symbol in {bar.symbol for bar in klines}:
        rows = rows_by_symbol.get(symbol, [])
        if not rows:
            reasons.append(f"metadata gap: {symbol}")
            continue
        covering = sorted(rows, key=lambda row: row.effective_from)
        for bar in bars_by_symbol[symbol]:
            match: ContractMetadata | None = None
            for row in covering:
                if row.effective_from <= bar.open_time:
                    match = row
                else:
                    break
            if match is None or not _size_knowable(match):
                reasons.append(f"metadata gap: {symbol} {canonical_datetime(bar.open_time)}")
                break
    return reasons


def macro_reasons(events: Sequence[MacroEvent]) -> list[str]:
    reasons: list[str] = []
    seen: dict[tuple[str, str, datetime, str], int] = defaultdict(int)
    for event in events:
        if not _is_utc(event.event_time) or not _is_utc(event.publication_time):
            reasons.append(f"non-monotonic timestamps: {event.source} {event.event_id}")
            continue
        key = (event.source, event.event_id, event.publication_time, event.vintage or "")
        seen[key] += 1
    for (source, event_id, publication_time, _), count in seen.items():
        if count > 1:
            reasons.append(f"duplicate macro: {source} {event_id} {canonical_datetime(publication_time)}")
    return reasons


def _derived_reasons(snapshot: DataSnapshot, computed_source: str) -> list[str]:
    reasons: list[str] = []
    bars = snapshot.hourly_bars + snapshot.daily_bars
    if not bars:
        return reasons
    for bar in bars:
        if bar.source_hash != computed_source:
            reasons.append("source hash cannot be reproduced")
            break
        expected = transformation_hash(computed_source, bar.transformation_version)
        if bar.transformation_hash != expected:
            reasons.append("source hash cannot be reproduced")
            break
    if snapshot.derived_data_hash and snapshot.derived_data_hash != derived_data_hash(bars):
        reasons.append("source hash cannot be reproduced")
    return reasons


def validate_snapshot(snapshot: DataSnapshot) -> DataResult:
    reasons: list[str] = []
    reasons.extend(kline_reasons(snapshot.klines_1m, require_all_symbols=True))
    reasons.extend(funding_reasons(snapshot.klines_1m, snapshot.funding))
    reasons.extend(metadata_reasons(snapshot.klines_1m, snapshot.metadata))
    reasons.extend(macro_reasons(snapshot.macro_events))
    if reasons:
        return blocked(*reasons)

    computed_source = source_hash(snapshot.klines_1m, snapshot.funding, snapshot.metadata)
    if snapshot.source_hash and snapshot.source_hash != computed_source:
        return blocked("source hash cannot be reproduced")
    reasons.extend(_derived_reasons(snapshot, computed_source))
    if reasons:
        return blocked(*reasons)

    computed_macro = macro_hash(snapshot.macro_events)
    if snapshot.macro_hash and snapshot.macro_hash != computed_macro:
        return blocked("source hash cannot be reproduced")
    return DataResult(
        status=OK,
        snapshot=DataSnapshot(
            klines_1m=snapshot.klines_1m,
            funding=snapshot.funding,
            metadata=snapshot.metadata,
            macro_events=snapshot.macro_events,
            hourly_bars=snapshot.hourly_bars,
            daily_bars=snapshot.daily_bars,
            source_hash=computed_source,
            derived_data_hash=snapshot.derived_data_hash,
            macro_hash=computed_macro,
            evaluation_start=snapshot.evaluation_start,
        ),
    )


def floor_utc(value: datetime, *, hour: bool = False, day: bool = False) -> datetime:
    aware = value.astimezone(UTC)
    if day:
        return datetime(aware.year, aware.month, aware.day, tzinfo=UTC)
    if hour:
        return aware.replace(minute=0, second=0, microsecond=0)
    return aware.replace(second=0, microsecond=0)


def first_completed_utc_day(start: datetime) -> datetime:
    # A partial UTC day cannot count toward EMA-200 daily warm-up.
    midnight = floor_utc(start, day=True)
    if start == midnight:
        return midnight
    return midnight + DAY
