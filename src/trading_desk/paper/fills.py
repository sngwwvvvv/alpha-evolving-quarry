from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_desk.backtest.account import SizedOrder, fill_price, round_to_step, size_order
from trading_desk.data.contracts import ContractMetadata
from trading_desk.strategy.models import (
    AGGREGATE_PLANNED_RISK,
    GROSS_LEVERAGE_CEILING,
    LONG,
    PER_POSITION_RISK,
    SYSTEM_LEVERAGE,
    ExecutionPolicy,
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    time: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


@dataclass(frozen=True, slots=True)
class TradePrint:
    symbol: str
    time: datetime
    price: Decimal
    quantity: Decimal


class FillAdapter:
    def __init__(self, policy: ExecutionPolicy, *, latency: timedelta = timedelta(0)) -> None:
        if latency < timedelta(0):
            raise ValueError("latency must be non-negative")
        self.policy = policy
        self.latency = latency

    def legal_size(self, **kwargs: Any) -> SizedOrder | None:
        return size_order(**kwargs)

    def conservative_fill(
        self,
        *,
        reference: Decimal,
        buy: bool,
        quantity: Decimal,
        metadata: ContractMetadata,
        books: Sequence[OrderBook] = (),
        trades: Sequence[TradePrint] = (),
        asof: datetime,
    ) -> Decimal:
        model = fill_price(reference, self.policy, metadata, buy=buy)
        cutoff = asof - self.latency
        observed: list[Decimal] = []
        for book in books:
            if book.time > cutoff:
                continue
            executable = _book_executable(book, buy=buy, quantity=quantity)
            if executable is not None:
                observed.append(executable)
        for trade in trades:
            if trade.time > cutoff:
                continue
            observed.append(trade.price)
        if not observed:
            return model
        if buy:
            raw = max(model, max(observed))
        else:
            raw = min(model, min(observed))
        return round_to_step(raw, metadata.price_tick, round_up=buy)

    def fit_executable(
        self,
        sized: SizedOrder,
        *,
        entry: Decimal,
        equity: Decimal,
        direction: str,
        metadata: ContractMetadata,
        open_planned_risk: Decimal,
        open_notional: Decimal,
    ) -> SizedOrder | None:
        buy = direction == LONG
        stop_fill = fill_price(sized.stop, self.policy, metadata, buy=not buy)
        quantity = sized.quantity
        remaining_aggregate = AGGREGATE_PLANNED_RISK * equity - open_planned_risk
        remaining_notional = GROSS_LEVERAGE_CEILING * equity - open_notional
        per_position = PER_POSITION_RISK * equity
        while quantity >= metadata.min_quantity:
            if direction == LONG:
                price_loss = entry - stop_fill
            else:
                price_loss = stop_fill - entry
            if price_loss <= 0:
                return None
            loss_per_qty = price_loss + entry * self.policy.fee_rate + stop_fill * self.policy.fee_rate
            planned = quantity * loss_per_qty
            notional = quantity * entry
            if (
                planned <= per_position
                and planned <= remaining_aggregate
                and notional <= remaining_notional
                and notional >= metadata.min_notional
            ):
                return SizedOrder(
                    quantity=quantity,
                    entry_price=entry,
                    stop=sized.stop,
                    take_profit=sized.take_profit,
                    planned_risk=planned,
                    notional=notional,
                    margin=notional / SYSTEM_LEVERAGE,
                )
            quantity -= metadata.quantity_step
            quantity = round_to_step(quantity, metadata.quantity_step)
        return None


def _book_executable(book: OrderBook, *, buy: bool, quantity: Decimal) -> Decimal | None:
    levels = book.asks if buy else book.bids
    if not levels:
        return None
    remaining = quantity
    worst: Decimal | None = None
    for level in levels:
        worst = level.price if worst is None else (max(worst, level.price) if buy else min(worst, level.price))
        remaining -= level.quantity
        if remaining <= 0:
            break
    return worst
