from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_desk.backtest.account import Account, LivePosition, PendingEntry, size_order
from trading_desk.backtest.execution import Fill, PaperLifecycle, RejectedOrder, _is_funding_slot
from trading_desk.config import SUPPORTED_SYMBOLS, UTC
from trading_desk.data.contracts import (
    HOURLY_TRANSFORMATION,
    MINUTE,
    TIMEFRAME_1H,
    ContractMetadata,
    DerivedBar,
    Funding,
    Kline1m,
)
from trading_desk.paper.feeds import DATA_STALE, FakeClock, PaperFeed
from trading_desk.paper.fills import FillAdapter
from trading_desk.state.approvals import ApprovalCommand
from trading_desk.state.db import Database, RunIdentity
from trading_desk.state.transitions import transition
from trading_desk.strategy.default import DefaultStrategy
from trading_desk.strategy.models import LONG, ExecutionPolicy, StrategySignal


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


def _unique(reasons: list[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return tuple(unique)


def _parse_dt(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _signal_payload(signal: StrategySignal) -> dict[str, str]:
    return {
        "bar_open_time": signal.bar_open_time.isoformat(),
        "close": format(signal.close, "f"),
        "direction": signal.direction,
        "published_at": signal.published_at.isoformat(),
        "stop": format(signal.stop, "f"),
        "symbol": signal.symbol,
        "take_profit": format(signal.take_profit, "f"),
    }


def _signal_from_payload(data: Mapping[str, Any]) -> StrategySignal:
    return StrategySignal(
        symbol=str(data["symbol"]),
        direction=str(data["direction"]),
        bar_open_time=_parse_dt(str(data["bar_open_time"])),
        published_at=_parse_dt(str(data["published_at"])),
        close=_dec(data["close"]),
        stop=_dec(data["stop"]),
        take_profit=_dec(data["take_profit"]),
    )


def _position_payload(position: LivePosition) -> dict[str, str]:
    return {
        "direction": position.direction,
        "entry_fee": format(position.entry_fee, "f"),
        "entry_price": format(position.entry_price, "f"),
        "entry_time": position.entry_time.isoformat(),
        "funding_pnl": format(position.funding_pnl, "f"),
        "margin": format(position.margin, "f"),
        "notional": format(position.notional, "f"),
        "planned_risk": format(position.planned_risk, "f"),
        "quantity": format(position.quantity, "f"),
        "stop": format(position.stop, "f"),
        "symbol": position.symbol,
        "take_profit": format(position.take_profit, "f"),
    }


def _position_from_payload(data: Mapping[str, Any]) -> LivePosition:
    return LivePosition(
        symbol=str(data["symbol"]),
        direction=str(data["direction"]),
        quantity=_dec(data["quantity"]),
        entry_price=_dec(data["entry_price"]),
        entry_time=_parse_dt(str(data["entry_time"])),
        stop=_dec(data["stop"]),
        take_profit=_dec(data["take_profit"]),
        planned_risk=_dec(data["planned_risk"]),
        notional=_dec(data["notional"]),
        margin=_dec(data["margin"]),
        entry_fee=_dec(data["entry_fee"]),
        funding_pnl=_dec(data.get("funding_pnl", "0")),
    )


def _account_payload(account: Account, marks: Mapping[str, Decimal]) -> dict[str, Any]:
    return {
        "balance": format(account.balance, "f"),
        "daily_paused": account.daily_paused,
        "day_start_equity": format(account.day_start_equity, "f"),
        "equity": format(account.equity(dict(marks)), "f"),
        "funding_pnl": format(account.funding_pnl, "f"),
        "halt_reason": account.halt_reason,
        "halted": account.halted,
        "high_water": format(account.high_water, "f"),
        "max_drawdown": format(account.max_drawdown, "f"),
        "pending": {
            symbol: {
                "fill_time": pending.fill_time.isoformat(),
                "signal": _signal_payload(pending.signal),
            }
            for symbol, pending in account.pending.items()
        },
        "positions": {symbol: _position_payload(position) for symbol, position in account.positions.items()},
        "starting_equity": format(account.starting_equity, "f"),
    }


def _restore_account(account: Account, payload: Mapping[str, Any]) -> None:
    account.starting_equity = _dec(payload["starting_equity"])
    account.balance = _dec(payload["balance"])
    account.high_water = _dec(payload["high_water"])
    account.day_start_equity = _dec(payload["day_start_equity"])
    account.max_drawdown = _dec(payload["max_drawdown"])
    account.halted = bool(payload.get("halted"))
    account.halt_reason = payload.get("halt_reason")
    account.daily_paused = bool(payload.get("daily_paused"))
    account.funding_pnl = _dec(payload.get("funding_pnl", "0"))
    account.positions = {
        symbol: _position_from_payload(row)
        for symbol, row in dict(payload.get("positions") or {}).items()
    }
    pending_rows = dict(payload.get("pending") or {})
    account.pending = {
        symbol: PendingEntry(
            _signal_from_payload(row["signal"]),
            fill_time=_parse_dt(str(row["fill_time"])),
        )
        for symbol, row in pending_rows.items()
    }


def _paper_hour_bar(symbol: str, hour_open: datetime, minutes: Sequence[Kline1m]) -> DerivedBar:
    bars = list(minutes)
    return DerivedBar(
        symbol=symbol,
        timeframe=TIMEFRAME_1H,
        open_time=hour_open,
        open=bars[0].open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=bars[-1].close,
        volume=sum((bar.volume for bar in bars), Decimal("0")),
        source_hash="paper",
        transformation_version=HOURLY_TRANSFORMATION,
        transformation_hash="paper",
    )


def _cover_minutes(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    have: set[datetime],
    reasons: list[str],
) -> None:
    if end < start:
        reasons.append("reversed timestamps")
        return
    stamp = start
    while stamp < end:
        if stamp not in have:
            reasons.append(f"unreconciled gap: {symbol}")
            return
        stamp += MINUTE


def reconcile_chronologically(
    *,
    intents: Sequence[LocalIntent],
    events: Sequence[MarketEvent],
    account: Account,
    start: datetime | None = None,
    end: datetime | None = None,
    symbols: Sequence[str] = SUPPORTED_SYMBOLS,
    windows: Mapping[str, tuple[datetime, datetime]] | None = None,
) -> ReconcileResult:
    reasons: list[str] = []
    klines = [event for event in events if event.bar is not None or event.kind == "kline"]
    seen: set[tuple[str, datetime]] = set()
    last_time: dict[str, datetime] = {}
    by_symbol: dict[str, list[MarketEvent]] = {}
    for event in klines:
        if not _is_utc(event.time):
            reasons.append(f"non-utc event: {event.symbol}")
        key = (event.symbol, event.time)
        if key in seen:
            reasons.append(f"duplicate event: {event.symbol}")
        seen.add(key)
        previous = last_time.get(event.symbol)
        if previous is not None and event.time < previous:
            reasons.append("reversed timestamps")
        last_time[event.symbol] = event.time
        by_symbol.setdefault(event.symbol, []).append(event)

    tape = {(event.symbol, event.time) for event in klines}

    def _in_symbol_window(symbol: str, stamp: datetime) -> bool:
        if windows is None:
            return True
        if symbol not in windows:
            return False
        win_start, win_end = windows[symbol]
        return win_start <= stamp < win_end

    for intent in intents:
        if not _in_symbol_window(intent.symbol, intent.time):
            continue
        if (intent.symbol, intent.time) not in tape:
            reasons.append(f"unreconciled intent: {intent.symbol}")

    intent_entries = {(intent.symbol, intent.kind) for intent in intents}
    for symbol, pending in account.pending.items():
        fill_time = pending.fill_time
        if windows is not None:
            in_window = _in_symbol_window(symbol, fill_time)
        elif start is not None and end is not None:
            in_window = start <= fill_time < end
        else:
            in_window = True
        if not in_window:
            continue
        if (symbol, "entry") not in intent_entries:
            reasons.append(f"unreconciled pending: {symbol}")
        elif (symbol, fill_time) not in tape:
            reasons.append(f"unreconciled pending: {symbol}")

    cover_windows: dict[str, tuple[datetime, datetime]]
    if windows is not None:
        cover_windows = dict(windows)
    elif start is not None and end is not None:
        cover_windows = {symbol: (start, end) for symbol in symbols}
    else:
        cover_windows = {}

    for symbol, (win_start, win_end) in cover_windows.items():
        have = {item.time for item in by_symbol.get(symbol, [])}
        _cover_minutes(symbol=symbol, start=win_start, end=win_end, have=have, reasons=reasons)

    for symbol in account.positions:
        if symbol not in symbols:
            continue
        if symbol in cover_windows:
            win_start, win_end = cover_windows[symbol]
            if win_start < win_end and symbol not in by_symbol:
                reasons.append(f"unreconciled position: {symbol}")

    timeline = tuple(sorted(klines, key=lambda item: (item.time, item.symbol, item.kind)))
    if reasons:
        return ReconcileResult(
            status="PAPER_DATA_GAP",
            reasons=_unique(reasons),
            events=timeline,
            repaired=False,
        )
    return ReconcileResult(status="OK", events=timeline, repaired=True)


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
        fitted = self._engine.fills.fit_executable(
            sized,
            entry=entry,
            equity=self._sizing_equity,
            direction=pending.signal.direction,
            metadata=metadata,
            open_planned_risk=self.account.open_planned_risk(),
            open_notional=self.account.open_notional(),
        )
        if fitted is None:
            self.rejected.append(RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason="risk_cap"))
            return
        fee = fitted.entry_price * fitted.quantity * self.policy.fee_rate
        self.account.balance -= fee
        self.account.positions[bar.symbol] = LivePosition(
            symbol=bar.symbol,
            direction=pending.signal.direction,
            quantity=fitted.quantity,
            entry_price=fitted.entry_price,
            entry_time=bar.open_time,
            stop=fitted.stop,
            take_profit=fitted.take_profit,
            planned_risk=fitted.planned_risk,
            notional=fitted.notional,
            margin=fitted.margin,
            entry_fee=fee,
        )
        self.fills.append(
            Fill(
                symbol=bar.symbol,
                quantity=fitted.quantity,
                price=fitted.entry_price,
                time=bar.open_time,
                fee=fee,
                reason="entry",
                planned_risk=fitted.planned_risk,
                notional=fitted.notional,
                margin=fitted.margin,
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
        fresh_start: bool = False,
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
        self._hydrated = False
        self._last_reconcile_ok = False
        self._fresh_start = fresh_start
        self._hour_minutes: dict[str, list[Kline1m]] = defaultdict(list)
        self._hourly_closes: dict[str, list[Decimal]] = defaultdict(list)
        self._daily_closes: dict[str, list[Decimal]] = defaultdict(list)
        self.lifecycle = WiredPaperLifecycle(
            self,
            policy=policy or fills.policy,
            starting_equity=starting_equity,
        )
        if fresh_start:
            self._hydrated = True
            self._last_reconcile_ok = True
        else:
            self._load_snapshot()

    def begin_session(self) -> None:
        if self._fresh_start:
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
        if not self._hydrated:
            return False
        if not self._fresh_start and not self._last_reconcile_ok:
            return False
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
        last = self._last_open.get(bar.symbol)
        if last is not None and bar.open_time <= last:
            self.lifecycle.rejected.append(
                RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason="duplicate kline")
            )
            return None
        result = self._repair_if_gap(bar)
        if result is not None and result.status == "PAPER_DATA_GAP":
            return result
        self.feed.ingest_kline(bar)
        self.lifecycle.begin_timestamp()
        meta = self._metadata.get(bar.symbol)
        if meta is None:
            self.lifecycle.rejected.append(RejectedOrder(symbol=bar.symbol, time=bar.open_time, reason="metadata gap"))
            return result
        self._drive_bar(bar, meta)
        self._last_reconcile_ok = True
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
        windows: dict[str, tuple[datetime, datetime]] = {}
        hole_symbols: list[str] = []
        for symbol in self.feed.symbols:
            last = self._last_open.get(symbol)
            symbol_start = last + MINUTE if last is not None else start
            if last is not None:
                symbol_start = max(symbol_start, start)
            if symbol_start >= end:
                continue
            hole_symbols.append(symbol)
            windows[symbol] = (symbol_start, end)
            for bar in self.feed.fetch_rest_klines(symbol, symbol_start, end):
                if last is not None and bar.open_time <= last:
                    continue
                events.append(MarketEvent(time=bar.open_time, symbol=bar.symbol, kind="kline", bar=bar))
        intents = tuple(
            LocalIntent(time=pending.fill_time, symbol=symbol, kind="entry")
            for symbol, pending in self.lifecycle.account.pending.items()
            if symbol in windows and windows[symbol][0] <= pending.fill_time < windows[symbol][1]
        )
        cover_symbols = tuple(hole_symbols) or tuple(self.feed.symbols)
        result = reconcile_chronologically(
            intents=intents,
            events=events,
            account=self.lifecycle.account,
            start=start if not windows else None,
            end=end if not windows else None,
            symbols=cover_symbols,
            windows=windows or None,
        )
        if result.status != "OK" or result.repaired is not True:
            self._gap_repaired = False
            self._enter_data_gap(result.reasons)
            return result
        for event in result.events:
            if event.bar is None:
                continue
            last = self._last_open.get(event.bar.symbol)
            if last is not None and event.bar.open_time <= last:
                continue
            meta = self._metadata.get(event.bar.symbol)
            if meta is None:
                continue
            self.lifecycle.begin_timestamp()
            self._drive_bar(event.bar, meta)
        self._gap_repaired = True
        self._unrepaired_gap = False
        self._last_reconcile_ok = True
        self._sync_run_state()
        return result

    def _enter_data_gap(self, reasons: tuple[str, ...]) -> None:
        self._gap_repaired = False
        self._unrepaired_gap = True
        self.lifecycle.account.pending.clear()
        _ = reasons
        if self._paper_status() == "PAPER_RUNNING":
            self._transition("PAPER_DATA_GAP", reason="unreconciled_gap")
        self._last_reconcile_ok = False
        self._persist_snapshot()

    def _sync_run_state(self) -> None:
        status = self._paper_status()
        if self.lifecycle.account.halted and status == "PAPER_RUNNING":
            self._transition("PAPER_MDD_HALTED", reason="mdd_stop")
        elif self.lifecycle.account.daily_paused and status == "PAPER_RUNNING":
            self._transition("PAPER_DAILY_PAUSED", reason="daily_loss")
        self._persist_snapshot()

    def maybe_resume_daily(self) -> str:
        state = self._transition("PAPER_RUNNING", reason="daily_resume")
        self.lifecycle.account.daily_paused = False
        self.lifecycle.on_day_open(self.clock.now())
        self.lifecycle.begin_timestamp()
        self._last_open.clear()
        self._last_reconcile_ok = True
        self._persist_snapshot()
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
        self._last_reconcile_ok = True
        self._persist_snapshot()
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
        self._last_reconcile_ok = True
        self._persist_snapshot()
        return state

    def _drive_bar(self, bar: Kline1m, meta: ContractMetadata) -> None:
        if bar.open_time.hour == 0 and bar.open_time.minute == 0:
            self.lifecycle.on_day_open(bar.open_time)
        funding = None
        rate = self.feed.funding_rate(bar.symbol)
        if rate is not None and _is_funding_slot(bar.open_time):
            funding = Funding(symbol=bar.symbol, funding_time=bar.open_time, funding_rate=rate)
        self.lifecycle.on_minute(bar, meta, funding)
        self._last_open[bar.symbol] = bar.open_time
        self._hour_minutes[bar.symbol].append(bar)
        if bar.open_time.minute != 59:
            return
        hour_open = bar.open_time.replace(minute=0, second=0, microsecond=0)
        minutes = [item for item in self._hour_minutes[bar.symbol] if item.open_time >= hour_open]
        if not minutes:
            minutes = [bar]
        hour_bar = _paper_hour_bar(bar.symbol, hour_open, minutes)
        self._hourly_closes[bar.symbol].append(hour_bar.close)
        if hour_open.hour == 23:
            self._daily_closes[bar.symbol].append(hour_bar.close)
        if self.may_open_risk():
            self.lifecycle.on_hour_complete(
                hour_bar,
                list(self._hourly_closes[bar.symbol]),
                list(self._daily_closes[bar.symbol]),
            )

    def _snapshot_payload(self) -> dict[str, Any]:
        existing: dict[str, Any] = {}
        try:
            row = self.db.get_paper_state(self.run.family_id)
        except ValueError:
            row = None
        if row is not None and row["payload_json"]:
            loaded = json.loads(row["payload_json"])
            if isinstance(loaded, dict):
                existing = loaded
        status = str(row["status"]) if row is not None else self.engine_status
        payload = dict(existing)
        payload["account"] = _account_payload(self.lifecycle.account, self.lifecycle._marks)
        payload["engine_status"] = self.engine_status
        payload["gap_repaired"] = self._gap_repaired
        payload["hydrated"] = True
        payload["last_open"] = {symbol: stamp.isoformat() for symbol, stamp in self._last_open.items()}
        payload["last_reconcile"] = "OK" if self._last_reconcile_ok and not self._unrepaired_gap else "PAPER_DATA_GAP"
        payload["marks"] = {symbol: format(price, "f") for symbol, price in self.lifecycle._marks.items()}
        payload["seq"] = self._seq
        payload["unrepaired_gap"] = self._unrepaired_gap
        if status == "PAPER_DATA_GAP":
            payload["repaired"] = "true" if self._gap_repaired else "false"
        return payload

    def _persist_snapshot(self) -> None:
        try:
            status = self._paper_status()
        except ValueError:
            return
        self.db.upsert_paper_state(
            family_id=self.run.family_id,
            status=status,
            payload=self._snapshot_payload(),
        )

    def _load_snapshot(self) -> None:
        try:
            row = self.db.get_paper_state(self.run.family_id)
        except ValueError:
            self._hydrated = True
            self._fresh_start = True
            self._last_reconcile_ok = True
            return
        raw = row.get("payload_json")
        if not raw:
            self._hydrated = True
            self._fresh_start = True
            self._last_reconcile_ok = True
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or "account" not in payload:
            self._hydrated = True
            self._fresh_start = True
            self._last_reconcile_ok = True
            return
        _restore_account(self.lifecycle.account, payload["account"])
        marks = payload.get("marks") or {}
        self.lifecycle._marks = {str(symbol): _dec(price) for symbol, price in dict(marks).items()}
        last_open = payload.get("last_open") or {}
        self._last_open = {str(symbol): _parse_dt(str(stamp)) for symbol, stamp in dict(last_open).items()}
        self._seq = int(payload.get("seq") or 0)
        self._gap_repaired = bool(payload.get("gap_repaired"))
        self._unrepaired_gap = bool(payload.get("unrepaired_gap"))
        self.engine_status = str(payload.get("engine_status") or self.engine_status)
        self._last_reconcile_ok = str(payload.get("last_reconcile")) == "OK" and not self._unrepaired_gap
        self._fresh_start = False
        self._hydrated = True
