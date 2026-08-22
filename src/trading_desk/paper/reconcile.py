from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_desk.backtest.account import Account, LivePosition, PendingEntry, size_order
from trading_desk.backtest.execution import Fill, PaperLifecycle, RejectedOrder
from trading_desk.config import SUPPORTED_SYMBOLS
from trading_desk.data.contracts import MINUTE, ContractMetadata, Kline1m
from trading_desk.paper.feeds import DATA_STALE, FakeClock, PaperFeed
from trading_desk.paper.fills import FillAdapter
from trading_desk.state.approvals import ApprovalCommand
from trading_desk.state.db import Database, RunIdentity
from trading_desk.state.transitions import transition
from trading_desk.strategy.default import DefaultStrategy
from trading_desk.strategy.models import LONG, SYSTEM_LEVERAGE, ExecutionPolicy, StrategySignal


@dataclass(frozen=True, slots=True)
class LocalIntent:
    time: datetime
    symbol: str
    kind: str
    quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MarketEvent:
    time: datetime
    symbol: str
    kind: str
    bar: Kline1m | None = None
    price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: str
    reasons: tuple[str, ...] = ()
    events: tuple[MarketEvent, ...] = ()
    repaired: bool = False


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def reconcile_chronologically(
    *,
    intents: Sequence[LocalIntent],
    events: Sequence[MarketEvent],
    account: Account,
    start: datetime | None = None,
    end: datetime | None = None,
    symbols: Sequence[str] = SUPPORTED_SYMBOLS,
) -> ReconcileResult:
    _ = (intents, account)
    reasons: list[str] = []
    klines = [event for event in events if event.bar is not None or event.kind == "kline"]
    timeline = sorted(klines, key=lambda item: (item.time, item.symbol, item.kind))
    seen: set[tuple[str, datetime]] = set()
    previous: datetime | None = None
    by_symbol: dict[str, list[MarketEvent]] = {}
    for event in timeline:
        if not _is_utc(event.time):
            reasons.append(f"non-utc event: {event.symbol}")
        key = (event.symbol, event.time)
        if key in seen:
            reasons.append(f"duplicate event: {event.symbol}")
        seen.add(key)
        if previous is not None and event.time < previous:
            reasons.append("reversed timestamps")
        previous = event.time
        by_symbol.setdefault(event.symbol, []).append(event)

    if start is not None and end is not None:
        if end < start:
            reasons.append("reversed timestamps")
        stamp_cursor = start
        while start <= stamp_cursor < end:
            for symbol in symbols:
                have = {item.time for item in by_symbol.get(symbol, [])}
                if stamp_cursor not in have:
                    reasons.append(f"unreconciled gap: {symbol}")
            if any(reason.startswith("unreconciled gap") for reason in reasons):
                break
            stamp_cursor += MINUTE

    if reasons:
        unique: list[str] = []
        for reason in reasons:
            if reason not in unique:
                unique.append(reason)
        return ReconcileResult(
            status="PAPER_DATA_GAP",
            reasons=tuple(unique),
            events=tuple(timeline),
            repaired=False,
        )
    return ReconcileResult(status="OK", events=tuple(timeline), repaired=True)


class WiredPaperLifecycle(PaperLifecycle):
    def __init__(
        self,
        engine: PaperEngine,
        *,
        strategy: DefaultStrategy | None = None,
        policy: ExecutionPolicy | None = None,
        starting_equity: Decimal | None = None,
    ) -> None:
        super().__init__(strategy=strategy, policy=policy, starting_equity=starting_equity)
        self._engine = engine

    def _try_enter(self, bar: Kline1m, metadata: ContractMetadata, pending: PendingEntry) -> None:
        self.account.pending.pop(bar.symbol, None)
        if not self._engine.may_open_risk():
            return
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
            self.rejected.append(RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason="min_notional"))
            return
        buy = pending.signal.direction == LONG
        entry = self._engine.fills.conservative_fill(
            reference=bar.open,
            buy=buy,
            quantity=sized.quantity,
            metadata=metadata,
            books=self._engine.feed.books(bar.symbol),
            trades=self._engine.feed.trade_prints(bar.symbol),
            asof=bar.open_time,
        )
        fee = entry * sized.quantity * self.policy.fee_rate
        notional = sized.quantity * entry
        margin = notional / SYSTEM_LEVERAGE
        self.account.balance -= fee
        self.account.positions[bar.symbol] = LivePosition(
            symbol=bar.symbol,
            direction=pending.signal.direction,
            quantity=sized.quantity,
            entry_price=entry,
            entry_time=bar.open_time,
            stop=sized.stop,
            take_profit=sized.take_profit,
            planned_risk=sized.planned_risk,
            notional=notional,
            margin=margin,
            entry_fee=fee,
        )
        self.fills.append(
            Fill(
                symbol=bar.symbol,
                quantity=sized.quantity,
                price=entry,
                time=bar.open_time,
                fee=fee,
                reason="entry",
                planned_risk=sized.planned_risk,
                notional=notional,
                margin=margin,
            )
        )


class PaperEngine:
    def __init__(
        self,
        *,
        db: Database,
        run: RunIdentity,
        feed: PaperFeed,
        fills: FillAdapter,
        clock: FakeClock,
        allowlist: Collection[str] = (),
        metadata: Mapping[str, ContractMetadata] | None = None,
        starting_equity: Decimal | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.db = db
        self.run = run
        self.feed = feed
        self.fills = fills
        self.clock = clock
        self.allowlist = frozenset(allowlist)
        self._metadata = dict(metadata or {})
        self.engine_status = "PAPER_RUNNING"
        self._seq = 0
        self._last_open: dict[str, datetime] = {}
        self._gap_repaired = False
        self._unrepaired_gap = False
        self.lifecycle = WiredPaperLifecycle(
            self,
            policy=policy or fills.policy,
            starting_equity=starting_equity,
        )

    def begin_session(self) -> None:
        self.lifecycle.on_day_open(self.clock.now())
        self.lifecycle.begin_timestamp()

    def _paper_status(self) -> str:
        return str(self.db.get_paper_state(self.run.family_id)["status"])

    def _transition(
        self,
        to_state: str,
        *,
        reason: str | None = None,
        approval: ApprovalCommand | None = None,
        repaired: bool = False,
    ) -> str:
        self._seq += 1
        return transition(
            self.db,
            run_id=self.run.run_id,
            to_state=to_state,
            idempotency_key=f"{self.run.run_id}:paper:{self._seq}:{to_state}",
            reason=reason,
            approval=approval,
            allowlist=self.allowlist,
            now=self.clock.now(),
            repaired=repaired,
        )

    def may_open_risk(self) -> bool:
        if self.engine_status == DATA_STALE or self._unrepaired_gap:
            return False
        if not self.feed.required_fresh(now=self.clock.now()):
            return False
        if self.lifecycle.account.halted or self.lifecycle.account.daily_paused:
            return False
        return self._paper_status() == "PAPER_RUNNING"

    def check_freshness(self) -> str:
        if self.feed.required_fresh(now=self.clock.now()) and not self._unrepaired_gap:
            if self.engine_status == DATA_STALE:
                self.engine_status = "PAPER_RUNNING"
            return self.engine_status
        self.engine_status = DATA_STALE
        self.lifecycle.account.pending.clear()
        return DATA_STALE

    def queue_signal(self, signal: StrategySignal) -> None:
        self.check_freshness()
        if not self.may_open_risk():
            return
        if signal.symbol in self.lifecycle.account.positions or signal.symbol in self.lifecycle.account.pending:
            return
        self.lifecycle.account.pending[signal.symbol] = PendingEntry(signal, fill_time=signal.published_at)

    def process_bar(self, bar: Kline1m) -> ReconcileResult | None:
        self.check_freshness()
        result = self._repair_if_gap(bar)
        if result is not None and result.status == "PAPER_DATA_GAP":
            return result
        self.feed.ingest_kline(bar)
        self.lifecycle.begin_timestamp()
        meta = self._metadata.get(bar.symbol)
        if meta is None:
            self.lifecycle.rejected.append(RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason="metadata gap"))
            return result
        self.lifecycle.on_minute(bar, meta, None)
        self._last_open[bar.symbol] = bar.open_time
        self._sync_run_state()
        return result

    def _repair_if_gap(self, bar: Kline1m) -> ReconcileResult | None:
        last = self._last_open.get(bar.symbol)
        if last is None:
            return None
        start = last + MINUTE
        if bar.open_time <= start:
            return None
        return self.repair_gaps(start=start, end=bar.open_time)

    def repair_gaps(self, *, start: datetime, end: datetime) -> ReconcileResult:
        events: list[MarketEvent] = []
        for symbol in self.feed.symbols:
            for bar in self.feed.fetch_rest_klines(symbol, start, end):
                events.append(MarketEvent(time=bar.open_time, symbol=bar.symbol, kind="kline", bar=bar))
        intents = tuple(
            LocalIntent(time=pending.fill_time, symbol=symbol, kind="entry")
            for symbol, pending in self.lifecycle.account.pending.items()
        )
        result = reconcile_chronologically(
            intents=intents,
            events=events,
            account=self.lifecycle.account,
            start=start,
            end=end,
            symbols=self.feed.symbols,
        )
        if result.status != "OK" or result.repaired is not True:
            self._gap_repaired = False
            self._enter_data_gap(result.reasons)
            return result
        for event in result.events:
            if event.bar is None:
                continue
            meta = self._metadata.get(event.bar.symbol)
            if meta is None:
                continue
            self.lifecycle.begin_timestamp()
            self.lifecycle.on_minute(event.bar, meta, None)
            self._last_open[event.bar.symbol] = event.bar.open_time
        self._gap_repaired = True
        self._unrepaired_gap = False
        self._sync_run_state()
        return result

    def _enter_data_gap(self, reasons: tuple[str, ...]) -> None:
        self._gap_repaired = False
        self._unrepaired_gap = True
        self.lifecycle.account.pending.clear()
        _ = reasons
        if self._paper_status() == "PAPER_RUNNING":
            self._transition("PAPER_DATA_GAP", reason="unreconciled_gap")

    def _sync_run_state(self) -> None:
        status = self._paper_status()
        if self.lifecycle.account.halted and status == "PAPER_RUNNING":
            self._transition("PAPER_MDD_HALTED", reason="mdd_stop")
        elif self.lifecycle.account.daily_paused and status == "PAPER_RUNNING":
            self._transition("PAPER_DAILY_PAUSED", reason="daily_loss")

    def maybe_resume_daily(self) -> str:
        state = self._transition("PAPER_RUNNING", reason="daily_resume")
        self.lifecycle.account.daily_paused = False
        self.lifecycle.on_day_open(self.clock.now())
        self.lifecycle.begin_timestamp()
        self._last_open.clear()
        return state

    def resume_after_mdd(self, approval: ApprovalCommand | None = None) -> str:
        state = self._transition("PAPER_RUNNING", approval=approval, reason="resume_mdd")
        account = self.lifecycle.account
        account.halted = False
        account.halt_reason = None
        equity = account.equity(self.lifecycle._marks)
        account.high_water = equity
        account.day_start_equity = equity
        account.max_drawdown = Decimal("0")
        account.daily_paused = False
        self._last_open.clear()
        return state

    def resume_after_data_gap(self, approval: ApprovalCommand, *, repaired: object | None = None) -> str:
        if repaired is None:
            flag = self._gap_repaired is True
        else:
            flag = repaired is True and self._gap_repaired is True
        state = self._transition(
            "PAPER_RUNNING",
            approval=approval,
            repaired=flag,
            reason="resume_data_gap",
        )
        self._unrepaired_gap = False
        self.engine_status = "PAPER_RUNNING"
        self._last_open.clear()
        return state
