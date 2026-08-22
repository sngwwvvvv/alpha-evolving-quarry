from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_desk.backtest.account import PendingEntry, fill_price
from trading_desk.backtest.execution import PaperLifecycle
from trading_desk.config import UTC, sha256_hex
from trading_desk.data.contracts import MINUTE, ContractMetadata, Kline1m
from trading_desk.paper.feeds import (
    ANALYSIS_ONLY_STREAMS,
    DATA_STALE,
    STALE_AFTER,
    FakeClock,
    FakeRestClient,
    PaperFeed,
    StreamUpdate,
)
from trading_desk.paper.fills import BookLevel, FillAdapter, OrderBook, TradePrint
from trading_desk.paper.reconcile import (
    LocalIntent,
    MarketEvent,
    PaperEngine,
    reconcile_chronologically,
)
from trading_desk.state.approvals import ApprovalCommand, ApprovalError, hash_approval_command
from trading_desk.state.db import Database, RunIdentity
from trading_desk.state.transitions import TransitionError, transition
from trading_desk.strategy.models import (
    GROSS_LEVERAGE_CEILING,
    LONG,
    PER_POSITION_RISK,
    SHORT,
    SYSTEM_LEVERAGE,
    ExecutionPolicy,
    StrategyParameters,
    StrategySignal,
)

COMMIT = "c" * 40
ALLOWLIST = frozenset({"user-1"})
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
POLICY = ExecutionPolicy()
STARTING_EQUITY = Decimal("10000")


def _digest(label: str) -> str:
    return sha256_hex(label)


def _run_hashes() -> dict[str, str]:
    return {
        "data_snapshot_hash": _digest("snapshot"),
        "derived_data_hash": _digest("derived"),
        "validation_policy_hash": _digest("validation-policy-v2"),
        "execution_policy_hash": _digest("execution"),
    }


_NONCES: dict[str, int] = {}


def version_nonce(family_id: str) -> int:
    _NONCES[family_id] = _NONCES.get(family_id, 0) + 1
    return _NONCES[family_id]


def _register_run(db: Database, *, family_id: str | None = None) -> tuple[str, str, RunIdentity]:
    family_id = family_id or db.create_family()
    version_id = db.register_version(
        family_id,
        code_commit=COMMIT,
        spec={"lookback": 20, "threshold": 1.5, "nonce": version_nonce(family_id)},
    )
    run = db.create_run(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=COMMIT,
        **_run_hashes(),
    )
    return family_id, version_id, run


def object_hashes(run: RunIdentity) -> dict[str, str]:
    return {
        "code_commit": run.code_commit,
        "data_snapshot_hash": run.data_snapshot_hash,
        "derived_data_hash": run.derived_data_hash,
        "execution_policy_hash": run.execution_policy_hash,
        "family_id": run.family_id,
        "run_id": run.run_id,
        "strategy_version_id": run.strategy_version_id,
        "validation_policy_hash": run.validation_policy_hash,
    }


def make_approval(
    run: RunIdentity,
    *,
    action: str,
    actor_id: str = "user-1",
    idempotency_key: str = "approve-1",
    timestamp: datetime = NOW,
) -> ApprovalCommand:
    hashes = object_hashes(run)
    digest = hash_approval_command(
        actor_id=actor_id,
        action=action,
        object_hashes=hashes,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )
    return ApprovalCommand(
        actor_id=actor_id,
        action=action,
        object_hashes=hashes,
        source_command_hash=digest,
        timestamp=timestamp,
        idempotency_key=idempotency_key,
    )


def apply(
    db: Database,
    run: RunIdentity,
    to_state: str,
    *,
    seq: int,
    now: datetime = NOW,
    **kwargs: object,
) -> str:
    return transition(
        db,
        run_id=run.run_id,
        to_state=to_state,
        idempotency_key=f"{run.run_id}:{seq}:{to_state}",
        now=now,
        **kwargs,
    )


def go_paper(db: Database, run: RunIdentity, *, now: datetime = NOW) -> None:
    path = ("DRAFT", "DEVELOPMENT_RUNNING", "OOS_RUNNING", "READY_FOR_PAPER")
    for seq, state in enumerate(path):
        apply(db, run, state, seq=seq, now=now)
    apply(
        db,
        run,
        "PAPER_RUNNING",
        seq=len(path),
        now=now,
        approval=make_approval(run, action="start_paper"),
        allowlist=ALLOWLIST,
    )


def _meta(
    symbol: str = "BTCUSDT",
    *,
    quantity_step: str = "0.001",
    min_quantity: str = "0.001",
    min_notional: str = "5",
    price_tick: str = "0.01",
) -> ContractMetadata:
    return ContractMetadata(
        symbol=symbol,
        effective_from=NOW,
        price_tick=Decimal(price_tick),
        quantity_step=Decimal(quantity_step),
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
        listing_state="TRADING",
    )


def _bar(
    symbol: str,
    open_time: datetime,
    price: str | Decimal = "100",
    *,
    high: str | Decimal | None = None,
    low: str | Decimal | None = None,
) -> Kline1m:
    px = Decimal(str(price))
    high_px = Decimal(str(high)) if high is not None else px
    low_px = Decimal(str(low)) if low is not None else px
    return Kline1m(
        symbol=symbol,
        open_time=open_time,
        open=px,
        high=high_px,
        low=low_px,
        close=px,
        volume=Decimal("1"),
    )


def _signal(symbol: str, stamp: datetime, *, direction: str = LONG, close: Decimal = Decimal("100")) -> StrategySignal:
    distance = close * Decimal("0.015")
    if direction == LONG:
        stop, take_profit = close - distance, close + distance * 2
    else:
        stop, take_profit = close + distance, close - distance * 2
    return StrategySignal(
        symbol=symbol,
        direction=direction,
        bar_open_time=stamp - MINUTE,
        published_at=stamp,
        close=close,
        stop=stop,
        take_profit=take_profit,
    )


def _book(
    symbol: str,
    stamp: datetime,
    *,
    bid: str = "99.90",
    ask: str = "100.10",
    quantity: str = "10",
) -> OrderBook:
    qty = Decimal(quantity)
    return OrderBook(
        symbol=symbol,
        time=stamp,
        bids=(BookLevel(Decimal(bid), qty),),
        asks=(BookLevel(Decimal(ask), qty),),
    )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        metadata: ContractMetadata | None = None,
        rest: FakeRestClient | None = None,
        latency: timedelta = timedelta(0),
        now: datetime = NOW,
        extra_symbols: tuple[str, ...] = (),
    ) -> None:
        self.db = Database(tmp_path / "state.sqlite3")
        self.family_id, self.version_id, self.run = _register_run(self.db)
        go_paper(self.db, self.run, now=now)
        self.clock = FakeClock(now)
        self.metadata = metadata or _meta()
        self.rest = rest if rest is not None else FakeRestClient()
        self.symbols = (self.metadata.symbol,) + extra_symbols
        metas = {self.metadata.symbol: self.metadata}
        for symbol in extra_symbols:
            metas[symbol] = _meta(symbol)
        self.feed = PaperFeed(
            clock=self.clock,
            rest=self.rest,
            symbols=self.symbols,
        )
        self.fills = FillAdapter(POLICY, latency=latency)
        self.engine = PaperEngine(
            db=self.db,
            run=self.run,
            feed=self.feed,
            fills=self.fills,
            clock=self.clock,
            allowlist=ALLOWLIST,
            metadata=metas,
            starting_equity=STARTING_EQUITY,
        )
        self.engine.begin_session()

    def arm(self, *, price: Decimal = Decimal("100")) -> None:
        now = self.clock.now()
        self.feed.ingest_account(now)
        for symbol in self.symbols:
            self.feed.ingest_price(symbol, now, price)

    def enter(
        self,
        *,
        price: str | Decimal = "100",
        direction: str = LONG,
        stamp: datetime | None = None,
    ) -> None:
        stamp = stamp or self.clock.now()
        symbol = self.metadata.symbol
        self.engine.queue_signal(_signal(symbol, stamp, direction=direction, close=Decimal(str(price))))
        self.engine.process_bar(_bar(symbol, stamp, price))


def test_required_streams_must_be_fresh_for_legal_entries(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.feed.required_fresh() is False
    h.enter()
    assert h.metadata.symbol not in h.engine.lifecycle.account.positions

    h.arm()
    assert h.feed.required_fresh() is True
    later = NOW + MINUTE
    h.clock.set(later)
    h.arm()
    h.enter(stamp=later)
    position = h.engine.lifecycle.account.positions[h.metadata.symbol]
    assert position.quantity > 0
    assert position.quantity % h.metadata.quantity_step == 0
    assert position.quantity * position.entry_price >= h.metadata.min_notional
    assert isinstance(h.engine.lifecycle, PaperLifecycle)


def test_legal_rounded_entries_and_min_notional_rejection(tmp_path: Path) -> None:
    stepped = _meta(quantity_step="0.1", min_quantity="0.1")
    h = Harness(tmp_path, metadata=stepped)
    h.arm()
    sized = h.fills.legal_size(
        equity=STARTING_EQUITY,
        direction=LONG,
        reference_price=Decimal("100"),
        parameters=StrategyParameters(),
        policy=POLICY,
        metadata=stepped,
        open_planned_risk=Decimal("0"),
        open_notional=Decimal("0"),
    )
    assert sized is not None
    assert sized.quantity == sized.quantity.quantize(Decimal("0.1"))
    assert sized.quantity % Decimal("0.1") == 0
    h.enter()
    fill = next(row for row in h.engine.lifecycle.fills if row.reason == "entry")
    assert fill.quantity % Decimal("0.1") == 0

    blocked_meta = _meta(min_notional="1000000")
    blocked = FillAdapter(POLICY).legal_size(
        equity=STARTING_EQUITY,
        direction=LONG,
        reference_price=Decimal("100"),
        parameters=StrategyParameters(),
        policy=POLICY,
        metadata=blocked_meta,
        open_planned_risk=Decimal("0"),
        open_notional=Decimal("0"),
    )
    assert blocked is None
    other = Harness(tmp_path / "blocked", metadata=blocked_meta)
    other.arm()
    other.enter()
    assert other.engine.lifecycle.account.positions == {}
    assert other.engine.lifecycle.rejected


def test_conservative_observable_fills_never_beat_book_after_latency() -> None:
    meta = _meta()
    adapter = FillAdapter(POLICY, latency=timedelta(seconds=1))
    model = fill_price(Decimal("100"), POLICY, meta, buy=True)
    delayed = _book("BTCUSDT", NOW - timedelta(seconds=1), ask="101.00")
    current = _book("BTCUSDT", NOW, ask="100.00")
    px = adapter.conservative_fill(
        reference=Decimal("100"),
        buy=True,
        quantity=Decimal("1"),
        metadata=meta,
        books=(delayed, current),
        trades=(
            TradePrint(
                symbol="BTCUSDT",
                time=NOW,
                price=Decimal("99.50"),
                quantity=Decimal("1"),
            ),
        ),
        asof=NOW,
    )
    assert px >= Decimal("101.00")
    assert px >= model
    assert px != Decimal("100.00")

    short_px = adapter.conservative_fill(
        reference=Decimal("100"),
        buy=False,
        quantity=Decimal("1"),
        metadata=meta,
        books=(_book("BTCUSDT", NOW - timedelta(seconds=1), bid="98.50"),),
        trades=(),
        asof=NOW,
    )
    short_model = fill_price(Decimal("100"), POLICY, meta, buy=False)
    assert short_px <= Decimal("98.50")
    assert short_px <= short_model


def test_conservative_fill_on_engine_uses_worse_observable_book(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.feed.ingest_book(_book(h.metadata.symbol, NOW, ask="101.20"))
    model = fill_price(Decimal("100"), POLICY, h.metadata, buy=True)
    h.enter()
    fill = next(row for row in h.engine.lifecycle.fills if row.reason == "entry")
    assert fill.price >= Decimal("101.20")
    assert fill.price >= model
    equity = STARTING_EQUITY
    assert fill.planned_risk <= equity * PER_POSITION_RISK
    assert fill.notional <= equity * GROSS_LEVERAGE_CEILING
    assert fill.notional / fill.margin == SYSTEM_LEVERAGE


def test_analysis_only_streams_do_not_change_signal_or_risk(tmp_path: Path) -> None:
    plain = Harness(tmp_path / "plain")
    tagged = Harness(tmp_path / "tagged")
    assert ANALYSIS_ONLY_STREAMS == frozenset({"liquidation", "long_short_ratio", "open_interest"})
    for stream in ANALYSIS_ONLY_STREAMS:
        tagged.feed.ingest(
            StreamUpdate(
                stream=stream,
                time=NOW,
                symbol="BTCUSDT",
                payload={"price": Decimal("1"), "ratio": Decimal("0.01"), "oi": Decimal("999999")},
            )
        )
    assert tagged.feed.required_fresh() is False
    assert {tag.stream for tag in tagged.feed.analysis_tags()} == ANALYSIS_ONLY_STREAMS

    for harness in (plain, tagged):
        harness.arm()
        harness.enter()

    def snapshot(engine: PaperEngine) -> tuple:
        fills = tuple((row.symbol, row.quantity, row.price, row.reason, row.planned_risk) for row in engine.lifecycle.fills)
        pos = engine.lifecycle.account.positions["BTCUSDT"]
        return fills, pos.quantity, pos.planned_risk, pos.notional, pos.entry_price

    assert snapshot(plain.engine) == snapshot(tagged.engine)
    assert plain.engine.lifecycle.account.equity({"BTCUSDT": Decimal("100")}) == tagged.engine.lifecycle.account.equity(
        {"BTCUSDT": Decimal("100")}
    )


def test_sixty_second_staleness_cancels_pending_blocks_new_risk_allows_reductions(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.engine.queue_signal(_signal(h.metadata.symbol, NOW))
    assert h.metadata.symbol in h.engine.lifecycle.account.pending

    h.clock.advance(STALE_AFTER + timedelta(seconds=1))
    assert h.engine.check_freshness() == DATA_STALE
    assert h.engine.engine_status == DATA_STALE
    assert h.engine.lifecycle.account.pending == {}
    assert h.engine.may_open_risk() is False
    h.engine.queue_signal(_signal(h.metadata.symbol, h.clock.now()))
    assert h.engine.lifecycle.account.pending == {}

    entered_at = NOW + timedelta(minutes=2)
    h.clock.set(entered_at)
    h.arm()
    assert h.engine.check_freshness() == "PAPER_RUNNING"
    h.enter(stamp=entered_at)
    assert h.metadata.symbol in h.engine.lifecycle.account.positions
    entries_before = [row for row in h.engine.lifecycle.fills if row.reason == "entry"]

    h.clock.advance(STALE_AFTER + timedelta(seconds=1))
    assert h.engine.check_freshness() == DATA_STALE
    h.engine.process_bar(_bar(h.metadata.symbol, entered_at + MINUTE, "98.4", high="98.4", low="98.4"))
    assert h.metadata.symbol not in h.engine.lifecycle.account.positions
    assert any(row.reason == "stop" for row in h.engine.lifecycle.fills)
    assert [row for row in h.engine.lifecycle.fills if row.reason == "entry"] == entries_before
    assert h.engine.may_open_risk() is False


def test_rest_gap_repair_replays_chronologically(tmp_path: Path) -> None:
    start = NOW + MINUTE
    gap = [NOW + MINUTE * offset for offset in (1, 2, 3, 4)]
    rest_bars = [_bar("BTCUSDT", stamp, "100") for stamp in gap]
    h = Harness(tmp_path, rest=FakeRestClient(rest_bars))
    h.arm()
    h.enter()
    live = NOW + MINUTE * 5
    h.clock.set(live)
    h.arm()
    result = h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert result is not None
    assert result.repaired is True
    assert result.status == "OK"
    times = [event.time for event in result.events if event.bar is not None]
    assert times == gap
    assert times == sorted(times)
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_RUNNING"


def test_unreconciled_gap_requires_repaired_bool_and_approval_not_payload_string(tmp_path: Path) -> None:
    rest = FakeRestClient([_bar("BTCUSDT", NOW + MINUTE, "100")], incomplete=True)
    h = Harness(tmp_path, rest=rest)
    h.arm()
    h.enter()
    live = NOW + MINUTE * 5
    h.clock.set(live)
    h.arm()
    result = h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert result is not None
    assert result.repaired is False
    assert result.status == "PAPER_DATA_GAP"
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_DATA_GAP"
    payload = json.loads(h.db.get_paper_state(h.run.family_id)["payload_json"])
    assert payload["repaired"] == "false"

    approval = make_approval(h.run, action="resume_data_gap", idempotency_key="resume-gap")
    h.db.upsert_paper_state(
        family_id=h.run.family_id,
        status="PAPER_DATA_GAP",
        payload={"repaired": "true"},
    )
    with pytest.raises(TransitionError, match="data gap not repaired"):
        h.engine.resume_after_data_gap(approval, repaired="true")
    with pytest.raises(TransitionError, match="data gap not repaired"):
        h.engine.resume_after_data_gap(approval, repaired=True)

    rest.incomplete = False
    rest.bars = [_bar("BTCUSDT", NOW + MINUTE * offset, "100") for offset in (1, 2, 3, 4)]
    repaired = h.engine.repair_gaps(start=NOW + MINUTE, end=live)
    assert repaired.repaired is True
    assert isinstance(repaired.repaired, bool)
    assert h.engine.resume_after_data_gap(approval) == "PAPER_RUNNING"
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_RUNNING"


def test_reconcile_chronologically_rejects_holes_and_accepts_sorted_repair() -> None:
    start = NOW
    end = NOW + MINUTE * 3
    events = (
        MarketEvent(time=NOW, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW, "100")),
        MarketEvent(time=NOW + MINUTE * 2, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE * 2, "100")),
    )
    hole = reconcile_chronologically(
        intents=(),
        events=events,
        account=PaperLifecycle().account,
        start=start,
        end=end,
        symbols=("BTCUSDT",),
    )
    assert hole.status == "PAPER_DATA_GAP"
    assert hole.repaired is False

    filled = (
        MarketEvent(time=NOW, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW, "100")),
        MarketEvent(time=NOW + MINUTE, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE, "101")),
        MarketEvent(time=NOW + MINUTE * 2, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE * 2, "102")),
    )
    intents = (LocalIntent(time=NOW + MINUTE, symbol="BTCUSDT", kind="entry", quantity=Decimal("0.1")),)
    ok = reconcile_chronologically(
        intents=intents,
        events=filled,
        account=PaperLifecycle().account,
        start=start,
        end=end,
        symbols=("BTCUSDT",),
    )
    assert ok.status == "OK"
    assert ok.repaired is True
    assert [event.time for event in ok.events] == [NOW, NOW + MINUTE, NOW + MINUTE * 2]


def test_daily_loss_resumes_automatically_next_utc_day(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.enter()
    crash = NOW + MINUTE
    h.clock.set(crash)
    h.arm(price=Decimal("90"))
    h.engine.process_bar(_bar(h.metadata.symbol, crash, "90", high="90", low="90"))
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_DAILY_PAUSED"
    assert h.engine.lifecycle.account.daily_paused
    assert h.metadata.symbol not in h.engine.lifecycle.account.positions

    with pytest.raises(TransitionError, match="daily pause not expired"):
        h.engine.maybe_resume_daily()
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_DAILY_PAUSED"

    h.clock.set(datetime(2026, 8, 24, 0, 0, tzinfo=UTC))
    assert h.engine.maybe_resume_daily() == "PAPER_RUNNING"
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_RUNNING"
    assert h.engine.lifecycle.account.daily_paused is False
    h.arm()
    h.enter(stamp=h.clock.now())
    assert h.metadata.symbol in h.engine.lifecycle.account.positions


def test_mdd_halt_requires_explicit_approval(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.enter()
    crash = NOW + MINUTE
    h.clock.set(crash)
    h.arm(price=Decimal("50"))
    h.engine.process_bar(_bar(h.metadata.symbol, crash, "50", high="50", low="50"))
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_MDD_HALTED"
    assert h.engine.lifecycle.account.halted
    assert h.metadata.symbol not in h.engine.lifecycle.account.positions

    with pytest.raises(TransitionError, match="approval required"):
        h.engine.resume_after_mdd()
    approval = make_approval(h.run, action="resume_mdd", idempotency_key="resume-mdd")
    assert h.engine.resume_after_mdd(approval) == "PAPER_RUNNING"
    assert h.engine.lifecycle.account.halted is False
    h.arm()
    h.enter(stamp=h.clock.now())
    assert h.metadata.symbol in h.engine.lifecycle.account.positions


def test_mdd_resume_rejects_superseded_version(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.enter()
    crash = NOW + MINUTE
    h.clock.set(crash)
    h.arm(price=Decimal("50"))
    h.engine.process_bar(_bar(h.metadata.symbol, crash, "50", high="50", low="50"))
    approval = make_approval(h.run, action="resume_mdd", idempotency_key="resume-mdd")
    _register_run(h.db, family_id=h.family_id)
    with pytest.raises(ApprovalError, match="superseded"):
        h.engine.resume_after_mdd(approval)


def test_data_gap_resume_rejects_superseded_version(tmp_path: Path) -> None:
    rest = FakeRestClient(incomplete=True)
    h = Harness(tmp_path, rest=rest)
    h.arm()
    h.enter()
    live = NOW + MINUTE * 3
    h.clock.set(live)
    h.arm()
    h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert h.db.get_paper_state(h.run.family_id)["status"] == "PAPER_DATA_GAP"
    rest.incomplete = False
    rest.bars = [_bar("BTCUSDT", NOW + MINUTE * offset, "100") for offset in (1, 2)]
    assert h.engine.repair_gaps(start=NOW + MINUTE, end=live).repaired is True
    _register_run(h.db, family_id=h.family_id)
    approval = make_approval(h.run, action="resume_data_gap", idempotency_key="gap-2")
    with pytest.raises(ApprovalError, match="superseded"):
        h.engine.resume_after_data_gap(approval)


def test_paper_modules_use_fake_clock_and_no_live_binance_network() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "trading_desk" / "paper"
    forbidden = (
        "binance",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "websockets",
        "websocket",
    )
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            item == needle or item.startswith(needle + ".") for item in imported for needle in forbidden
        ), path.name
    clock = FakeClock(NOW)
    clock.advance(timedelta(seconds=5))
    assert clock.now() == NOW + timedelta(seconds=5)


def _spy_minutes(engine: PaperEngine) -> list[tuple[str, datetime]]:
    applied: list[tuple[str, datetime]] = []
    real = engine.lifecycle.on_minute

    def wrapped(bar: Kline1m, metadata: ContractMetadata, funding: object) -> None:
        applied.append((bar.symbol, bar.open_time))
        real(bar, metadata, funding)

    engine.lifecycle.on_minute = wrapped  # type: ignore[method-assign]
    return applied


def test_process_bar_skips_duplicate_or_older_kline(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    applied = _spy_minutes(h.engine)
    h.enter()
    last = h.engine._last_open[h.metadata.symbol]
    assert applied == [(h.metadata.symbol, last)]
    h.engine.process_bar(_bar(h.metadata.symbol, last, "101"))
    h.engine.process_bar(_bar(h.metadata.symbol, last - MINUTE, "99"))
    assert h.engine._last_open[h.metadata.symbol] == last
    assert applied == [(h.metadata.symbol, last)]
    assert any("duplicate" in row.reason for row in h.engine.lifecycle.rejected)
    nxt = last + MINUTE
    h.clock.set(nxt)
    h.arm()
    h.engine.process_bar(_bar(h.metadata.symbol, nxt, "100"))
    assert h.engine._last_open[h.metadata.symbol] == nxt
    assert applied[-1] == (h.metadata.symbol, nxt)


def test_repair_does_not_replay_watermarked_minutes_for_other_symbols(tmp_path: Path) -> None:
    gap = [NOW + MINUTE * offset for offset in (1, 2, 3, 4)]
    rest_bars = [_bar("BTCUSDT", stamp, "100") for stamp in gap] + [_bar("ETHUSDT", stamp, "100") for stamp in gap]
    h = Harness(tmp_path, rest=FakeRestClient(rest_bars), extra_symbols=("ETHUSDT",))
    h.arm()
    applied = _spy_minutes(h.engine)
    h.engine.process_bar(_bar("BTCUSDT", NOW, "100"))
    h.engine.process_bar(_bar("ETHUSDT", NOW, "100"))
    for offset in (1, 2, 3):
        stamp = NOW + MINUTE * offset
        h.clock.set(stamp)
        h.arm()
        h.engine.process_bar(_bar("ETHUSDT", stamp, "100"))
    assert h.engine._last_open["ETHUSDT"] == NOW + MINUTE * 3
    assert h.engine._last_open["BTCUSDT"] == NOW
    eth_before = [stamp for symbol, stamp in applied if symbol == "ETHUSDT"]
    live = NOW + MINUTE * 5
    h.clock.set(live)
    h.arm()
    result = h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert result is not None and result.repaired is True
    eth_after = [stamp for symbol, stamp in applied if symbol == "ETHUSDT"]
    assert eth_after[: len(eth_before)] == eth_before
    assert eth_after.count(NOW + MINUTE) == 1
    assert eth_after.count(NOW + MINUTE * 2) == 1
    assert eth_after.count(NOW + MINUTE * 3) == 1
    assert NOW + MINUTE * 4 in eth_after
    assert h.engine._last_open["ETHUSDT"] == NOW + MINUTE * 4
    assert h.engine._last_open["BTCUSDT"] == live


def test_data_gap_resume_preserves_cursor_with_open_position(tmp_path: Path) -> None:
    rest = FakeRestClient(incomplete=True)
    h = Harness(tmp_path, rest=rest)
    h.arm()
    h.enter()
    assert h.metadata.symbol in h.engine.lifecycle.account.positions
    live = NOW + MINUTE * 5
    h.clock.set(live)
    h.arm()
    result = h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert result is not None and result.status == "PAPER_DATA_GAP"
    assert h.metadata.symbol in h.engine.lifecycle.account.positions
    rest.incomplete = False
    rest.bars = [_bar("BTCUSDT", NOW + MINUTE * offset, "100") for offset in (1, 2, 3, 4)]
    repaired = h.engine.repair_gaps(start=NOW + MINUTE, end=live)
    assert repaired.repaired is True
    watermark = h.engine._last_open[h.metadata.symbol]
    assert watermark == NOW + MINUTE * 4
    approval = make_approval(h.run, action="resume_data_gap", idempotency_key="resume-open")
    assert h.engine.resume_after_data_gap(approval) == "PAPER_RUNNING"
    assert h.engine._last_open[h.metadata.symbol] == watermark
    assert h.metadata.symbol in h.engine.lifecycle.account.positions
    applied = _spy_minutes(h.engine)
    h.clock.set(live)
    h.arm()
    h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert h.engine._last_open[h.metadata.symbol] == live
    assert applied == [("BTCUSDT", live)]
    h.engine.process_bar(_bar("BTCUSDT", live, "100"))
    assert applied == [("BTCUSDT", live)]


def test_reconcile_fails_closed_on_intents_and_account(tmp_path: Path) -> None:
    start = NOW
    end = NOW + MINUTE * 3
    tape = tuple(
        MarketEvent(time=NOW + MINUTE * offset, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE * offset, "100"))
        for offset in range(3)
    )
    missing_intent = reconcile_chronologically(
        intents=(LocalIntent(time=NOW + MINUTE * 9, symbol="BTCUSDT", kind="entry", quantity=Decimal("0.1")),),
        events=tape,
        account=PaperLifecycle().account,
        start=start,
        end=end,
        symbols=("BTCUSDT",),
    )
    assert missing_intent.status == "PAPER_DATA_GAP"
    assert missing_intent.repaired is False
    assert any("intent" in reason for reason in missing_intent.reasons)

    orphan = PaperLifecycle()
    orphan.account.pending["BTCUSDT"] = PendingEntry(_signal("BTCUSDT", NOW + MINUTE), fill_time=NOW + MINUTE)
    pending_mismatch = reconcile_chronologically(
        intents=(),
        events=tape,
        account=orphan.account,
        start=start,
        end=end,
        symbols=("BTCUSDT",),
    )
    assert pending_mismatch.status == "PAPER_DATA_GAP"
    assert any("pending" in reason for reason in pending_mismatch.reasons)

    held = PaperLifecycle()
    h = Harness(tmp_path)
    h.arm()
    h.enter()
    held.account = h.engine.lifecycle.account
    eth_only = tuple(
        MarketEvent(time=NOW + MINUTE * offset, symbol="ETHUSDT", kind="kline", bar=_bar("ETHUSDT", NOW + MINUTE * offset, "100"))
        for offset in range(3)
    )
    missing_position = reconcile_chronologically(
        intents=(),
        events=eth_only,
        account=held.account,
        start=start,
        end=end,
        symbols=("BTCUSDT",),
    )
    assert missing_position.status == "PAPER_DATA_GAP"
    assert any("position" in reason for reason in missing_position.reasons)


def test_reconcile_rejects_reversed_and_duplicate_input_without_sorting() -> None:
    reversed_events = (
        MarketEvent(time=NOW + MINUTE * 2, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE * 2, "102")),
        MarketEvent(time=NOW + MINUTE, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE, "101")),
        MarketEvent(time=NOW, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW, "100")),
    )
    reversed_result = reconcile_chronologically(
        intents=(),
        events=reversed_events,
        account=PaperLifecycle().account,
        start=NOW,
        end=NOW + MINUTE * 3,
        symbols=("BTCUSDT",),
    )
    assert reversed_result.status == "PAPER_DATA_GAP"
    assert reversed_result.repaired is False
    assert any("reversed" in reason for reason in reversed_result.reasons)

    duplicates = (
        MarketEvent(time=NOW, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW, "100")),
        MarketEvent(time=NOW + MINUTE, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE, "101")),
        MarketEvent(time=NOW + MINUTE * 2, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE * 2, "102")),
        MarketEvent(time=NOW + MINUTE, symbol="BTCUSDT", kind="kline", bar=_bar("BTCUSDT", NOW + MINUTE, "101")),
    )
    duplicate_result = reconcile_chronologically(
        intents=(),
        events=duplicates,
        account=PaperLifecycle().account,
        start=NOW,
        end=NOW + MINUTE * 3,
        symbols=("BTCUSDT",),
    )
    assert duplicate_result.status == "PAPER_DATA_GAP"
    assert any("duplicate" in reason for reason in duplicate_result.reasons)


def test_extreme_conservative_fill_is_rejected_when_caps_cannot_hold(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.arm()
    h.feed.ingest_book(_book(h.metadata.symbol, NOW, ask="1000000"))
    h.enter()
    assert h.metadata.symbol not in h.engine.lifecycle.account.positions
    assert h.engine.lifecycle.rejected
