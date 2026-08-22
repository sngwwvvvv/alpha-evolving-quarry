from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from trading_desk.config import UTC, canonical_json, sha256_hex

DATA_BLOCKED = "DATA_BLOCKED"
OK = "OK"

EMA_200_WARMUP_DAYS = 200
FUNDING_INTERVAL = timedelta(hours=8)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

HOURLY_TRANSFORMATION = "aggregate-1h-v1"
DAILY_TRANSFORMATION = "aggregate-utc-daily-v1"
TIMEFRAME_1H = "1h"
TIMEFRAME_1D = "1d"


@dataclass(frozen=True, slots=True)
class Kline1m:
    symbol: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class Funding:
    symbol: str
    funding_time: datetime
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class ContractMetadata:
    symbol: str
    effective_from: datetime
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    listing_state: str


@dataclass(frozen=True, slots=True)
class MacroEvent:
    source: str
    event_id: str
    event_time: datetime
    publication_time: datetime
    actual: Decimal
    unit: str
    vintage: str | None = None


@dataclass(frozen=True, slots=True)
class DerivedBar:
    symbol: str
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_hash: str
    transformation_version: str
    transformation_hash: str


@dataclass(frozen=True, slots=True)
class DataSnapshot:
    klines_1m: tuple[Kline1m, ...]
    funding: tuple[Funding, ...]
    metadata: tuple[ContractMetadata, ...]
    macro_events: tuple[MacroEvent, ...] = ()
    hourly_bars: tuple[DerivedBar, ...] = ()
    daily_bars: tuple[DerivedBar, ...] = ()
    source_hash: str = ""
    derived_data_hash: str = ""
    macro_hash: str = ""
    evaluation_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class DataResult:
    status: str
    reasons: tuple[str, ...] = ()
    snapshot: DataSnapshot | None = None
    bars: tuple[DerivedBar, ...] = ()
    evaluation_start: datetime | None = None


def canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_decimal(value: Decimal) -> str:
    return format(value, "f")


def hash_payload(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def transformation_hash(source_hash: str, transformation_version: str) -> str:
    return hash_payload(
        {
            "source_hash": source_hash,
            "transformation_version": transformation_version,
        }
    )


def canonical_kline(bar: Kline1m) -> dict[str, str]:
    return {
        "close": canonical_decimal(bar.close),
        "high": canonical_decimal(bar.high),
        "low": canonical_decimal(bar.low),
        "open": canonical_decimal(bar.open),
        "open_time": canonical_datetime(bar.open_time),
        "symbol": bar.symbol,
        "volume": canonical_decimal(bar.volume),
    }


def canonical_funding(row: Funding) -> dict[str, str]:
    return {
        "funding_rate": canonical_decimal(row.funding_rate),
        "funding_time": canonical_datetime(row.funding_time),
        "symbol": row.symbol,
    }


def canonical_metadata(row: ContractMetadata) -> dict[str, str]:
    return {
        "effective_from": canonical_datetime(row.effective_from),
        "listing_state": row.listing_state,
        "min_notional": canonical_decimal(row.min_notional),
        "min_quantity": canonical_decimal(row.min_quantity),
        "price_tick": canonical_decimal(row.price_tick),
        "quantity_step": canonical_decimal(row.quantity_step),
        "symbol": row.symbol,
    }


def canonical_macro(event: MacroEvent) -> dict[str, str | None]:
    return {
        "actual": canonical_decimal(event.actual),
        "event_id": event.event_id,
        "event_time": canonical_datetime(event.event_time),
        "publication_time": canonical_datetime(event.publication_time),
        "source": event.source,
        "unit": event.unit,
        "vintage": event.vintage,
    }


def canonical_derived(bar: DerivedBar) -> dict[str, str]:
    return {
        "close": canonical_decimal(bar.close),
        "high": canonical_decimal(bar.high),
        "low": canonical_decimal(bar.low),
        "open": canonical_decimal(bar.open),
        "open_time": canonical_datetime(bar.open_time),
        "source_hash": bar.source_hash,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "transformation_hash": bar.transformation_hash,
        "transformation_version": bar.transformation_version,
        "volume": canonical_decimal(bar.volume),
    }


def _sorted_map(items: Iterable[Any], canonical, keys) -> list[Mapping[str, Any]]:
    return [canonical(item) for item in sorted(items, key=keys)]


def source_hash(
    klines: Sequence[Kline1m],
    funding: Sequence[Funding],
    metadata: Sequence[ContractMetadata],
) -> str:
    return hash_payload(
        {
            "funding": _sorted_map(
                funding, canonical_funding, lambda row: (row.symbol, row.funding_time)
            ),
            "klines_1m": _sorted_map(
                klines, canonical_kline, lambda bar: (bar.symbol, bar.open_time)
            ),
            "metadata": _sorted_map(
                metadata,
                canonical_metadata,
                lambda row: (row.symbol, row.effective_from),
            ),
        }
    )


def macro_hash(events: Sequence[MacroEvent]) -> str:
    return hash_payload(
        _sorted_map(
            events,
            canonical_macro,
            lambda event: (
                event.source,
                event.event_id,
                event.publication_time,
                event.vintage or "",
            ),
        )
    )


def derived_data_hash(bars: Sequence[DerivedBar]) -> str:
    return hash_payload(
        _sorted_map(
            bars,
            canonical_derived,
            lambda bar: (bar.symbol, bar.timeframe, bar.open_time),
        )
    )


def blocked(*reasons: str) -> DataResult:
    return DataResult(status=DATA_BLOCKED, reasons=tuple(reasons))
