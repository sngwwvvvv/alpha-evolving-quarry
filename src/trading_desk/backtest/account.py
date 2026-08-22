from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from trading_desk.data.contracts import ContractMetadata
from trading_desk.strategy.models import (
    AGGREGATE_PLANNED_RISK,
    GROSS_LEVERAGE_CEILING,
    LONG,
    MDD_HALT,
    PER_POSITION_RISK,
    SHORT,
    SYSTEM_LEVERAGE,
    ExecutionPolicy,
    Position,
    StrategyParameters,
    StrategySignal,
)


def apply_slippage(price: Decimal, rate: Decimal, *, buy: bool) -> Decimal:
    if buy:
        return price * (1 + rate)
    return price * (1 - rate)


def round_to_step(value: Decimal, step: Decimal, *, round_up: bool = False) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    rounding = ROUND_UP if round_up else ROUND_DOWN
    return (value / step).to_integral_value(rounding=rounding) * step


def fill_price(price: Decimal, policy: ExecutionPolicy, metadata: ContractMetadata, *, buy: bool) -> Decimal:
    slipped = apply_slippage(price, policy.slippage_rate, buy=buy)
    return round_to_step(slipped, metadata.price_tick, round_up=buy)


@dataclass(frozen=True, slots=True)
class SizedOrder:
    quantity: Decimal
    entry_price: Decimal
    stop: Decimal
    take_profit: Decimal
    planned_risk: Decimal
    notional: Decimal
    margin: Decimal


@dataclass(frozen=True, slots=True)
class PendingEntry:
    signal: StrategySignal
    fill_time: datetime


@dataclass
class LivePosition:
    symbol: str
    direction: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    stop: Decimal
    take_profit: Decimal
    planned_risk: Decimal
    notional: Decimal
    margin: Decimal
    entry_fee: Decimal
    funding_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    def snapshot(self) -> Position:
        return Position(
            symbol=self.symbol,
            direction=self.direction,
            quantity=self.quantity,
            entry_price=self.entry_price,
            entry_time=self.entry_time,
            stop=self.stop,
            take_profit=self.take_profit,
            planned_risk=self.planned_risk,
            notional=self.notional,
            margin=self.margin,
        )


def size_order(
    *,
    equity: Decimal,
    direction: str,
    reference_price: Decimal,
    parameters: StrategyParameters,
    policy: ExecutionPolicy,
    metadata: ContractMetadata,
    open_planned_risk: Decimal,
    open_notional: Decimal,
) -> SizedOrder | None:
    if equity <= 0 or reference_price <= 0:
        return None
    buy = direction == LONG
    entry = fill_price(reference_price, policy, metadata, buy=buy)
    distance = entry * parameters.stop_pct
    target = distance * parameters.take_profit_r
    if direction == LONG:
        stop = entry - distance
        take_profit = entry + target
    else:
        stop = entry + distance
        take_profit = entry - target
    stop = round_to_step(stop, metadata.price_tick, round_up=not buy)
    take_profit = round_to_step(take_profit, metadata.price_tick, round_up=not buy)
    stop_fill = fill_price(stop, policy, metadata, buy=not buy)
    if direction == LONG:
        price_loss = entry - stop_fill
    else:
        price_loss = stop_fill - entry
    if price_loss <= 0:
        return None
    loss_per_qty = price_loss + entry * policy.fee_rate + stop_fill * policy.fee_rate
    remaining_aggregate = AGGREGATE_PLANNED_RISK * equity - open_planned_risk
    budget = min(PER_POSITION_RISK * equity, remaining_aggregate)
    if budget <= 0 or loss_per_qty <= 0:
        return None
    remaining_notional = GROSS_LEVERAGE_CEILING * equity - open_notional
    if remaining_notional <= 0:
        return None
    quantity = budget / loss_per_qty
    quantity = min(quantity, remaining_notional / entry)
    quantity = round_to_step(quantity, metadata.quantity_step)
    if quantity < metadata.min_quantity:
        return None
    if quantity * entry < metadata.min_notional:
        return None
    planned = quantity * loss_per_qty
    while planned > budget and quantity >= metadata.min_quantity:
        quantity -= metadata.quantity_step
        planned = quantity * loss_per_qty
    if quantity < metadata.min_quantity or quantity * entry < metadata.min_notional:
        return None
    if planned > PER_POSITION_RISK * equity or planned > remaining_aggregate:
        return None
    notional = quantity * entry
    if notional > remaining_notional:
        return None
    margin = notional / SYSTEM_LEVERAGE
    return SizedOrder(
        quantity=quantity,
        entry_price=entry,
        stop=stop,
        take_profit=take_profit,
        planned_risk=planned,
        notional=notional,
        margin=margin,
    )


class Account:
    def __init__(self, starting_equity: Decimal) -> None:
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        self.starting_equity = starting_equity
        self.balance = starting_equity
        self.positions: dict[str, LivePosition] = {}
        self.pending: dict[str, PendingEntry] = {}
        self.high_water = starting_equity
        self.day_start_equity = starting_equity
        self.max_drawdown = Decimal("0")
        self.halted = False
        self.halt_reason: str | None = None
        self.daily_paused = False
        self.funding_pnl = Decimal("0")

    def unrealized(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, position in self.positions.items():
            mark = marks.get(symbol, position.entry_price)
            if position.direction == LONG:
                total += (mark - position.entry_price) * position.quantity
            else:
                total += (position.entry_price - mark) * position.quantity
        return total

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.balance + self.unrealized(marks)

    def open_planned_risk(self) -> Decimal:
        return sum((position.planned_risk for position in self.positions.values()), Decimal("0"))

    def open_notional(self) -> Decimal:
        return sum((position.notional for position in self.positions.values()), Decimal("0"))

    def drawdown(self, equity: Decimal) -> Decimal:
        if self.high_water <= 0:
            return Decimal("0")
        drop = self.high_water - equity
        if drop <= 0:
            return Decimal("0")
        return drop / self.high_water

    def should_halt(self, equity: Decimal) -> bool:
        return self.drawdown(equity) >= MDD_HALT

    def note_equity(self, equity: Decimal) -> None:
        if equity > self.high_water:
            self.high_water = equity
        drawdown = self.drawdown(equity)
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
