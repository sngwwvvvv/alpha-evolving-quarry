from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_desk.backtest.account import (
    Account,
    LivePosition,
    PendingEntry,
    fill_price,
    size_order,
)
from trading_desk.config import SUPPORTED_SYMBOLS
from trading_desk.data.aggregate import derive_hourly_bars
from trading_desk.data.contracts import (
    DATA_BLOCKED,
    DAY,
    EMA_200_WARMUP_DAYS,
    MINUTE,
    ContractMetadata,
    DataSnapshot,
    DerivedBar,
    Funding,
    Kline1m,
)
from trading_desk.data.validate import validate_snapshot
from trading_desk.strategy.default import DefaultStrategy
from trading_desk.strategy.models import (
    DAILY_LOSS_STOP,
    LONG,
    MDD_FAIL,
    OK,
    SHORT,
    ExecutionPolicy,
)


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    quantity: Decimal
    price: Decimal
    time: datetime
    fee: Decimal
    reason: str
    planned_risk: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    margin: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    direction: str
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    fees: Decimal
    funding: Decimal
    planned_risk: Decimal
    exit_reason: str
    gross_pnl: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class RejectedOrder:
    symbol: str
    time: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    status: str
    reasons: tuple[str, ...] = ()
    trades: tuple[Trade, ...] = ()
    fills: tuple[Fill, ...] = ()
    rejected: tuple[RejectedOrder, ...] = ()
    ending_equity: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    equity_high_water: Decimal = Decimal("0")
    halted: bool = False
    daily_paused: bool = False
    funding_pnl: Decimal = Decimal("0")


def _is_funding_slot(stamp: datetime) -> bool:
    return stamp.minute == 0 and stamp.second == 0 and stamp.microsecond == 0 and stamp.hour in (0, 8, 16)


def _coverage_reasons(snapshot: DataSnapshot) -> list[str]:
    reasons: list[str] = []
    if snapshot.evaluation_start is None:
        reasons.append("evaluation_start is required")
        return reasons
    for symbol in SUPPORTED_SYMBOLS:
        count = sum(1 for bar in snapshot.daily_bars if bar.symbol == symbol)
        if count < EMA_200_WARMUP_DAYS:
            reasons.append(f"insufficient daily bars: {symbol}")
    present = {bar.symbol for bar in snapshot.klines_1m}
    for symbol in SUPPORTED_SYMBOLS:
        if symbol not in present:
            reasons.append(f"missing bar: {symbol}")
    return reasons


def _metadata_at(rows: list[ContractMetadata], stamp: datetime) -> ContractMetadata | None:
    match: ContractMetadata | None = None
    for row in rows:
        if row.effective_from <= stamp:
            match = row
        else:
            break
    return match


class PaperLifecycle:
    def __init__(
        self,
        strategy: DefaultStrategy | None = None,
        policy: ExecutionPolicy | None = None,
        starting_equity: Decimal | None = None,
    ) -> None:
        self.strategy = strategy or DefaultStrategy()
        self.policy = policy or ExecutionPolicy()
        self.account = Account(starting_equity or Decimal("10000"))
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.rejected: list[RejectedOrder] = []
        self._marks: dict[str, Decimal] = {}
        self._sizing_equity = self.account.starting_equity
        self._metadata: dict[str, list[ContractMetadata]] = {}

    def begin_timestamp(self) -> None:
        self._sizing_equity = self.account.equity(self._marks)

    def on_day_open(self, stamp: datetime) -> None:
        if self.account.halted:
            return
        if self.account.daily_paused:
            self.account.daily_paused = False
        self.account.day_start_equity = self.account.equity(self._marks)

    def on_minute(self, bar: Kline1m, metadata: ContractMetadata, funding: Funding | None) -> None:
        if bar.symbol not in self._marks:
            self._marks[bar.symbol] = bar.open
        if funding is not None and _is_funding_slot(bar.open_time):
            self._apply_funding(bar, funding)
        pending = self.account.pending.get(bar.symbol)
        if pending is not None and pending.fill_time == bar.open_time:
            self._try_enter(bar, metadata, pending)
        self._marks[bar.symbol] = bar.open
        self._enforce_stops(bar, metadata, mark=bar.open)
        self._manage_exits(bar, metadata)
        self._marks[bar.symbol] = bar.close
        self._enforce_stops(bar, metadata, mark=bar.close)

    def on_hour_complete(
        self,
        hour_bar: DerivedBar,
        hourly_closes: list[Decimal],
        daily_closes: list[Decimal],
    ) -> None:
        if self.account.halted or self.account.daily_paused:
            return
        symbol = hour_bar.symbol
        if symbol in self.account.positions or symbol in self.account.pending:
            return
        signal = self.strategy.signal(symbol, hour_bar, hourly_closes, daily_closes)
        if signal is None:
            return
        self.account.pending[symbol] = PendingEntry(signal, fill_time=signal.published_at)

    def _apply_funding(self, bar: Kline1m, funding: Funding) -> None:
        position = self.account.positions.get(bar.symbol)
        if position is None:
            return
        payment = position.quantity * bar.open * funding.funding_rate
        if position.direction == LONG:
            self.account.balance -= payment
            position.funding_pnl -= payment
            self.account.funding_pnl -= payment
        else:
            self.account.balance += payment
            position.funding_pnl += payment
            self.account.funding_pnl += payment

    def _try_enter(self, bar: Kline1m, metadata: ContractMetadata, pending: PendingEntry) -> None:
        self.account.pending.pop(bar.symbol, None)
        if self.account.halted or self.account.daily_paused or bar.symbol in self.account.positions:
            return
        sized = size_order(
            equity=self._sizing_equity,
            direction=pending.signal.direction,
            reference_price=bar.open,
            parameters=self.strategy.parameters,
            policy=self.policy,
            metadata=metadata,
            open_planned_risk=self.account.open_planned_risk(),
            open_notional=self.account.open_notional(),
        )
        if sized is None:
            reason = "min_notional"
            if metadata.min_notional > 0:
                reason = "min_notional"
            self.rejected.append(RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason=reason))
            return
        fee = sized.entry_price * sized.quantity * self.policy.fee_rate
        self.account.balance -= fee
        self.account.positions[bar.symbol] = LivePosition(
            symbol=bar.symbol,
            direction=pending.signal.direction,
            quantity=sized.quantity,
            entry_price=sized.entry_price,
            entry_time=bar.open_time,
            stop=sized.stop,
            take_profit=sized.take_profit,
            planned_risk=sized.planned_risk,
            notional=sized.notional,
            margin=sized.margin,
            entry_fee=fee,
        )
        self.fills.append(
            Fill(
                symbol=bar.symbol,
                quantity=sized.quantity,
                price=sized.entry_price,
                time=bar.open_time,
                fee=fee,
                reason="entry",
                planned_risk=sized.planned_risk,
                notional=sized.notional,
                margin=sized.margin,
            )
        )

    def _enforce_stops(self, bar: Kline1m, metadata: ContractMetadata, mark: Decimal) -> None:
        self._marks[bar.symbol] = mark
        equity = self.account.equity(self._marks)
        self.account.note_equity(equity)
        if self.account.halted:
            return
        if self.account.should_halt(equity):
            self._flatten_all(bar.open_time, bar.symbol, metadata, mark, "mdd_halt")
            self._cancel_pending()
            self.account.halted = True
            self.account.halt_reason = MDD_FAIL
            return
        day_start = self.account.day_start_equity
        if self.account.daily_paused or day_start <= 0:
            return
        loss = (day_start - equity) / day_start
        if loss >= DAILY_LOSS_STOP:
            self._flatten_all(bar.open_time, bar.symbol, metadata, mark, "daily_stop")
            self._cancel_pending()
            self.account.daily_paused = True

    def _cancel_pending(self) -> None:
        self.account.pending.clear()

    def _manage_exits(self, bar: Kline1m, metadata: ContractMetadata) -> None:
        position = self.account.positions.get(bar.symbol)
        if position is None:
            return
        if position.direction == LONG:
            hit_sl = bar.low <= position.stop
            hit_tp = bar.high >= position.take_profit
        else:
            hit_sl = bar.high >= position.stop
            hit_tp = bar.low <= position.take_profit
        if hit_sl:
            self._close(bar, metadata, position, position.stop, "stop")
        elif hit_tp:
            self._close(bar, metadata, position, position.take_profit, "take_profit")

    def _flatten_all(
        self,
        time: datetime,
        current_symbol: str,
        current_metadata: ContractMetadata,
        current_mark: Decimal,
        reason: str,
    ) -> None:
        for symbol in list(self.account.positions):
            position = self.account.positions.get(symbol)
            if position is None:
                continue
            if symbol == current_symbol:
                mark = current_mark
                metadata = current_metadata
            else:
                mark = self._marks.get(symbol, position.entry_price)
                rows = self._metadata.get(symbol, [])
                metadata = _metadata_at(rows, time) or current_metadata
            dummy = Kline1m(
                symbol=symbol,
                open_time=time,
                open=mark,
                high=mark,
                low=mark,
                close=mark,
                volume=Decimal("0"),
            )
            self._close(dummy, metadata, position, mark, reason)

    def _close(
        self,
        bar: Kline1m,
        metadata: ContractMetadata,
        position: LivePosition,
        level: Decimal,
        reason: str,
    ) -> None:
        buy = position.direction == SHORT
        price = fill_price(level, self.policy, metadata, buy=buy)
        fee = price * position.quantity * self.policy.fee_rate
        if position.direction == LONG:
            gross = (price - position.entry_price) * position.quantity
        else:
            gross = (position.entry_price - price) * position.quantity
        self.account.balance += gross - fee
        self.account.positions.pop(position.symbol, None)
        fees = position.entry_fee + fee
        self.fills.append(
            Fill(
                symbol=position.symbol,
                quantity=position.quantity,
                price=price,
                time=bar.open_time,
                fee=fee,
                reason=reason,
            )
        )
        self.trades.append(
            Trade(
                symbol=position.symbol,
                direction=position.direction,
                quantity=position.quantity,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=bar.open_time,
                exit_price=price,
                fees=fees,
                funding=position.funding_pnl,
                planned_risk=position.planned_risk,
                exit_reason=reason,
                gross_pnl=gross,
                net_pnl=gross + position.funding_pnl - fees,
            )
        )


class BacktestEngine(PaperLifecycle):
    def run(
        self,
        snapshot: DataSnapshot,
        strategy: DefaultStrategy,
        policy: ExecutionPolicy,
        starting_equity: Decimal,
    ) -> BacktestResult:
        reasons = _coverage_reasons(snapshot)
        if reasons:
            return BacktestResult(status=DATA_BLOCKED, reasons=tuple(reasons))
        checked = validate_snapshot(snapshot)
        if checked.status != OK or checked.snapshot is None:
            return BacktestResult(status=DATA_BLOCKED, reasons=checked.reasons)
        snapshot = checked.snapshot
        hourly = snapshot.hourly_bars
        if not hourly:
            derived = derive_hourly_bars(snapshot.klines_1m, source_hash=snapshot.source_hash or None)
            if derived.status != OK:
                return BacktestResult(status=DATA_BLOCKED, reasons=derived.reasons)
            hourly = derived.bars

        self.strategy = strategy
        self.policy = policy
        self.account = Account(starting_equity)
        self.fills = []
        self.trades = []
        self.rejected = []
        self._marks = {}
        self._sizing_equity = starting_equity

        metadata_rows: dict[str, list[ContractMetadata]] = defaultdict(list)
        for row in snapshot.metadata:
            metadata_rows[row.symbol].append(row)
        for rows in metadata_rows.values():
            rows.sort(key=lambda item: item.effective_from)
        self._metadata = dict(metadata_rows)

        klines_by_time: dict[datetime, list[Kline1m]] = defaultdict(list)
        for bar in snapshot.klines_1m:
            klines_by_time[bar.open_time].append(bar)

        funding_map: dict[tuple[str, datetime], Funding] = {}
        for row in snapshot.funding:
            if _is_funding_slot(row.funding_time):
                funding_map[(row.symbol, row.funding_time)] = row

        hourly_map: dict[tuple[str, datetime], DerivedBar] = {
            (bar.symbol, bar.open_time): bar for bar in hourly
        }
        hourly_series: dict[str, list[DerivedBar]] = defaultdict(list)
        for bar in sorted(hourly, key=lambda item: (item.symbol, item.open_time)):
            hourly_series[bar.symbol].append(bar)
        daily_series: dict[str, list[DerivedBar]] = defaultdict(list)
        for bar in sorted(snapshot.daily_bars, key=lambda item: (item.symbol, item.open_time)):
            daily_series[bar.symbol].append(bar)

        start = snapshot.evaluation_start
        for stamp in sorted(klines_by_time):
            if start is not None and stamp < start:
                continue
            if self.account.halted:
                break
            if stamp.hour == 0 and stamp.minute == 0:
                self.on_day_open(stamp)
            self.begin_timestamp()
            bars = sorted(klines_by_time[stamp], key=lambda item: SUPPORTED_SYMBOLS.index(item.symbol))
            for bar in bars:
                meta = _metadata_at(self._metadata.get(bar.symbol, []), stamp)
                if meta is None:
                    self.rejected.append(RejectedOrder(symbol=bar.symbol, time=stamp, reason="metadata gap"))
                    continue
                funding = funding_map.get((bar.symbol, stamp)) if _is_funding_slot(stamp) else None
                self.on_minute(bar, meta, funding)
                if self.account.halted:
                    break
            if self.account.halted:
                break
            if stamp.minute == 59:
                hour_open = stamp.replace(minute=0, second=0, microsecond=0)
                completed_at = stamp + MINUTE
                for symbol in SUPPORTED_SYMBOLS:
                    hour_bar = hourly_map.get((symbol, hour_open))
                    if hour_bar is None:
                        continue
                    hourly_closes = [
                        row.close for row in hourly_series[symbol] if row.open_time <= hour_open
                    ]
                    daily_closes = [
                        row.close for row in daily_series[symbol] if row.open_time + DAY <= completed_at
                    ]
                    self.on_hour_complete(hour_bar, hourly_closes, daily_closes)

        ending = self.account.equity(self._marks)
        self.account.note_equity(ending)
        status = MDD_FAIL if self.account.halted else OK
        return BacktestResult(
            status=status,
            trades=tuple(self.trades),
            fills=tuple(self.fills),
            rejected=tuple(self.rejected),
            ending_equity=ending,
            max_drawdown=self.account.max_drawdown,
            equity_high_water=self.account.high_water,
            halted=self.account.halted,
            daily_paused=self.account.daily_paused,
            funding_pnl=self.account.funding_pnl,
        )
