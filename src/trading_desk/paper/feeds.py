from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.contracts import Funding, Kline1m

STALE_AFTER = timedelta(seconds=60)
DATA_STALE = "DATA_STALE"
REQUIRED_STREAMS = frozenset({"price", "account"})
ANALYSIS_ONLY_STREAMS = frozenset({"liquidation", "long_short_ratio", "open_interest"})
FILL_STREAMS = frozenset({"order_book", "trades"})


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value.astimezone(UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = require_utc(now)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, now: datetime) -> datetime:
        self._now = require_utc(now)
        return self._now


@dataclass(frozen=True, slots=True)
class StreamUpdate:
    stream: str
    time: datetime
    symbol: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", require_utc(self.time))
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class AnalysisTag:
    stream: str
    time: datetime
    symbol: str | None
    payload: Mapping[str, Any]


class RestClient(Protocol):
    def fetch_klines(self, symbol: str, start: datetime, end: datetime) -> Sequence[Kline1m]: ...


class FakeRestClient:
    def __init__(self, bars: Sequence[Kline1m] = (), *, incomplete: bool = False) -> None:
        self.bars = list(bars)
        self.incomplete = incomplete
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch_klines(self, symbol: str, start: datetime, end: datetime) -> list[Kline1m]:
        self.calls.append((symbol, start, end))
        rows = [bar for bar in self.bars if bar.symbol == symbol and start <= bar.open_time < end]
        rows.sort(key=lambda bar: bar.open_time)
        if self.incomplete:
            return rows[:-1] if rows else []
        return rows


class PaperFeed:
    def __init__(
        self,
        clock: FakeClock,
        *,
        rest: RestClient | None = None,
        symbols: Sequence[str] = SUPPORTED_SYMBOLS,
    ) -> None:
        self.clock = clock
        self.rest = rest
        self.symbols = tuple(symbols)
        self._last: dict[tuple[str, str | None], datetime] = {}
        self._tags: list[AnalysisTag] = []
        self._books: dict[str, list[Any]] = {}
        self._trades: dict[str, list[Any]] = {}
        self._prices: dict[str, Decimal] = {}
        self._funding: dict[str, Funding] = {}

    def ingest(self, update: StreamUpdate) -> None:
        if update.stream in ANALYSIS_ONLY_STREAMS:
            self._tags.append(
                AnalysisTag(
                    stream=update.stream,
                    time=update.time,
                    symbol=update.symbol,
                    payload=dict(update.payload),
                )
            )
            return
        if update.stream == "order_book":
            book = update.payload.get("book")
            if book is not None:
                self.ingest_book(book)
            return
        if update.stream == "trades":
            trade = update.payload.get("trade")
            if trade is not None:
                self.ingest_trade(trade)
            return
        key = (update.stream, update.symbol)
        existing = self._last.get(key)
        if existing is None or update.time >= existing:
            self._last[key] = update.time
        if update.stream == "price" and update.symbol is not None and "price" in update.payload:
            self._prices[update.symbol] = Decimal(str(update.payload["price"]))

    def ingest_account(self, time: datetime) -> None:
        self.ingest(StreamUpdate(stream="account", time=time))

    def ingest_price(self, symbol: str, time: datetime, price: Decimal) -> None:
        self.ingest(StreamUpdate(stream="price", time=time, symbol=symbol, payload={"price": price}))

    def ingest_kline(self, bar: Kline1m) -> None:
        self.ingest_price(bar.symbol, self.clock.now(), bar.close)

    def ingest_funding(self, funding: Funding) -> None:
        self._funding[funding.symbol] = funding

    def funding_rate(self, symbol: str) -> Decimal | None:
        row = self._funding.get(symbol)
        return None if row is None else row.funding_rate

    def ingest_book(self, book: Any) -> None:
        self._books.setdefault(book.symbol, []).append(book)
        self._last[("order_book", book.symbol)] = book.time

    def ingest_trade(self, trade: Any) -> None:
        self._trades.setdefault(trade.symbol, []).append(trade)
        self._last[("trades", trade.symbol)] = trade.time

    def last_seen(self, stream: str, symbol: str | None = None) -> datetime | None:
        return self._last.get((stream, symbol))

    def is_fresh(self, stream: str, *, symbol: str | None = None, now: datetime | None = None) -> bool:
        now = now or self.clock.now()
        last = self.last_seen(stream, symbol)
        if last is None:
            return False
        return now - last < STALE_AFTER

    def required_fresh(self, *, now: datetime | None = None) -> bool:
        now = now or self.clock.now()
        if not self.is_fresh("account", now=now):
            return False
        for symbol in self.symbols:
            if not self.is_fresh("price", symbol=symbol, now=now):
                return False
        return True

    def analysis_tags(self) -> tuple[AnalysisTag, ...]:
        return tuple(self._tags)

    def books(self, symbol: str) -> tuple[Any, ...]:
        return tuple(self._books.get(symbol, ()))

    def trade_prints(self, symbol: str) -> tuple[Any, ...]:
        return tuple(self._trades.get(symbol, ()))

    def fetch_rest_klines(self, symbol: str, start: datetime, end: datetime) -> list[Kline1m]:
        if self.rest is None:
            return []
        return list(self.rest.fetch_klines(symbol, start, end))
