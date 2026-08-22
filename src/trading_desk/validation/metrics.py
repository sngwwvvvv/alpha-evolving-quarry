from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from trading_desk.config import SUPPORTED_SYMBOLS
from trading_desk.strategy.models import StrategyParameters
from trading_desk.validation.walk_forward import in_half_open, month_span, walk_forward_windows

ZERO = Decimal("0")
ONE = Decimal("1")


def as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    net_pnl: Decimal
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    gross_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "net_pnl", as_decimal(self.net_pnl))
        object.__setattr__(self, "fees", as_decimal(self.fees))
        object.__setattr__(self, "funding", as_decimal(self.funding))
        object.__setattr__(self, "slippage_cost", as_decimal(self.slippage_cost))
        if self.gross_pnl is None:
            inferred = self.net_pnl - self.funding + self.fees + self.slippage_cost
            object.__setattr__(self, "gross_pnl", inferred)
        else:
            object.__setattr__(self, "gross_pnl", as_decimal(self.gross_pnl))


@dataclass(frozen=True, slots=True)
class EquityPoint:
    time: datetime
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity", as_decimal(self.equity))


@dataclass(frozen=True, slots=True)
class SurvivalCase:
    total_return: Decimal
    profit_factor: Decimal
    max_drawdown: Decimal
    parameter: str | None = None
    left_out_symbol: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_return", as_decimal(self.total_return))
        object.__setattr__(self, "profit_factor", as_decimal(self.profit_factor))
        object.__setattr__(self, "max_drawdown", as_decimal(self.max_drawdown))


@dataclass(frozen=True, slots=True)
class EvaluationArtifacts:
    trades: tuple[TradeRecord, ...]
    starting_equity: Decimal
    start: datetime
    end: datetime
    equity_curve: tuple[EquityPoint, ...] = ()
    neighborhood: tuple[SurvivalCase, ...] = ()
    leave_one_out: tuple[SurvivalCase, ...] = ()
    trial_daily_returns: tuple[tuple[tuple[datetime, Decimal], ...], ...] = ()
    mutation_count: int = 0
    prior_version_id: str | None = None
    technical_error: bool = False
    technical_kind: str | None = None
    family_id: str | None = None
    strategy_version_id: str | None = None
    performance_evaluated_versions: int = 1
    parameters: StrategyParameters | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "starting_equity", as_decimal(self.starting_equity))
        object.__setattr__(self, "trades", tuple(self.trades))
        object.__setattr__(self, "equity_curve", tuple(self.equity_curve))
        object.__setattr__(self, "neighborhood", tuple(self.neighborhood))
        object.__setattr__(self, "leave_one_out", tuple(self.leave_one_out))


def profit_factor(pnls: Sequence[Decimal]) -> Decimal:
    values = [as_decimal(item) for item in pnls]
    if not values:
        return ZERO
    gross_profit = sum((item for item in values if item > 0), ZERO)
    gross_loss = abs(sum((item for item in values if item < 0), ZERO))
    if gross_loss == 0:
        return Decimal("Infinity") if gross_profit > 0 else ONE
    return gross_profit / gross_loss


def max_drawdown(equities: Sequence[Decimal]) -> Decimal:
    if not equities:
        return ZERO
    peak = equities[0]
    worst = ZERO
    for equity in equities:
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def reconstruct_equity_curve(artifacts: EvaluationArtifacts) -> tuple[EquityPoint, ...]:
    if artifacts.equity_curve:
        return artifacts.equity_curve
    equity = artifacts.starting_equity
    points = [EquityPoint(artifacts.start, equity)]
    for trade in sorted(artifacts.trades, key=lambda row: (row.exit_time, row.symbol)):
        equity += trade.net_pnl
        points.append(EquityPoint(trade.exit_time, equity))
    if points[-1].time != artifacts.end:
        points.append(EquityPoint(artifacts.end, equity))
    return tuple(points)


def equity_at(curve: Sequence[EquityPoint], when: datetime, starting: Decimal) -> Decimal:
    last = starting
    for point in curve:
        if point.time <= when:
            last = point.equity
        else:
            break
    return last


def total_return(start_equity: Decimal, end_equity: Decimal) -> Decimal:
    if start_equity <= 0:
        return ZERO
    return end_equity / start_equity - ONE


def cagr(start_equity: Decimal, end_equity: Decimal, start: datetime, end: datetime) -> Decimal:
    seconds = Decimal(str((end - start).total_seconds()))
    years = seconds / Decimal(str(365.25 * 24 * 3600))
    if years <= 0 or start_equity <= 0:
        return ZERO
    if end_equity <= 0:
        return Decimal("-1")
    ratio = float(end_equity / start_equity)
    return as_decimal(ratio ** (1.0 / float(years)) - 1.0)


def calmar_ratio(cagr_value: Decimal, drawdown: Decimal) -> Decimal:
    if drawdown > 0:
        return cagr_value / drawdown
    if cagr_value > 0:
        return Decimal("Infinity")
    return ZERO


def window_equities(
    curve: Sequence[EquityPoint],
    start: datetime,
    end: datetime,
    starting: Decimal,
) -> list[Decimal]:
    open_eq = starting
    for point in curve:
        if point.time < start:
            open_eq = point.equity
        else:
            break
    values = [open_eq]
    for point in curve:
        if in_half_open(point.time, start, end):
            values.append(point.equity)
    return values


def positive_contribution_share(totals: Mapping[str, Decimal]) -> dict[str, Decimal]:
    positive = {key: max(value, ZERO) for key, value in totals.items()}
    denom = sum(positive.values(), ZERO)
    if denom == 0:
        return {key: ONE for key in positive}
    return {key: value / denom for key, value in positive.items()}


def group_net_pnl(trades: Sequence[TradeRecord], key_fn) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for trade in trades:
        key = str(key_fn(trade))
        totals[key] = totals.get(key, ZERO) + trade.net_pnl
    return totals


def stressed_net_pnl(trade: TradeRecord, stress: Mapping[str, Any]) -> Decimal:
    fee_mult = as_decimal(stress["fee_multiplier"])
    slip_mult = as_decimal(stress["slippage_multiplier"])
    paid_mult = as_decimal(stress["adverse_paid_funding_multiplier"])
    recv_mult = as_decimal(stress["received_funding_multiplier"])
    if trade.funding < 0:
        stressed_funding = trade.funding * paid_mult
    elif trade.funding > 0:
        stressed_funding = trade.funding * recv_mult
    else:
        stressed_funding = ZERO
    return (
        trade.net_pnl
        - trade.fees * (fee_mult - ONE)
        - trade.slippage_cost * (slip_mult - ONE)
        + (stressed_funding - trade.funding)
    )


def case_survives(case: SurvivalCase, survival: Mapping[str, Any]) -> bool:
    return (
        case.total_return >= as_decimal(survival["total_return_minimum"])
        and case.profit_factor >= as_decimal(survival["profit_factor_minimum"])
        and case.max_drawdown <= as_decimal(survival["max_drawdown_maximum"])
    )


def perturbed_parameters(params: StrategyParameters) -> tuple[StrategyParameters, ...]:
    lookback = params.hourly_ema_lookback
    lo_lb = max(1, int(round(lookback * 0.9)))
    hi_lb = max(1, int(round(lookback * 1.1)))
    ten = Decimal("0.1")
    return (
        replace(params, hourly_ema_lookback=lo_lb),
        replace(params, hourly_ema_lookback=hi_lb),
        replace(params, stop_pct=params.stop_pct * (ONE - ten)),
        replace(params, stop_pct=params.stop_pct * (ONE + ten)),
        replace(params, take_profit_r=params.take_profit_r * (ONE - ten)),
        replace(params, take_profit_r=params.take_profit_r * (ONE + ten)),
    )


def _week_start(stamp: datetime) -> datetime:
    monday = stamp.date() - timedelta(days=stamp.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=stamp.tzinfo)


def sampled_returns(
    curve: Sequence[EquityPoint],
    start: datetime,
    end: datetime,
    starting: Decimal,
    step: timedelta,
    origin: datetime,
) -> list[tuple[datetime, Decimal]]:
    times: list[datetime] = []
    cursor = origin
    while cursor < start:
        cursor += step
    times.append(start)
    while cursor <= end:
        if cursor > start:
            times.append(cursor)
        cursor += step
    if times[-1] != end:
        times.append(end)
    unique: list[datetime] = []
    for item in times:
        if not unique or unique[-1] != item:
            unique.append(item)
    out: list[tuple[datetime, Decimal]] = []
    for idx in range(1, len(unique)):
        prev = equity_at(curve, unique[idx - 1], starting)
        current = equity_at(curve, unique[idx], starting)
        if prev == 0:
            ret = ZERO
        else:
            ret = current / prev - ONE
        out.append((unique[idx], ret))
    return out


def compute_metrics(
    artifacts: EvaluationArtifacts,
    policy: Any,
    *,
    kind: str,
    trial_count: int,
) -> dict[str, Any]:
    from trading_desk.validation.statistics import (
        annualized_psr_benchmark,
        deflated_sharpe_ratio,
        probabilistic_sharpe_ratio,
        trade_entry_moving_block_lower_bound,
    )

    document = policy.document
    curve = reconstruct_equity_curve(artifacts)
    start_eq = equity_at(curve, artifacts.start, artifacts.starting_equity)
    end_eq = equity_at(curve, artifacts.end, artifacts.starting_equity)
    ret = total_return(start_eq, end_eq)
    mdd = max_drawdown([point.equity for point in curve] or [start_eq])
    growth = cagr(start_eq, end_eq, artifacts.start, artifacts.end)
    trades = artifacts.trades
    counts = {symbol: 0 for symbol in SUPPORTED_SYMBOLS}
    for trade in trades:
        counts[trade.symbol] = counts.get(trade.symbol, 0) + 1
    pnls = [trade.net_pnl for trade in trades]
    pf = profit_factor(pnls)
    weekly = sampled_returns(
        curve,
        artifacts.start,
        artifacts.end,
        artifacts.starting_equity,
        timedelta(days=7),
        _week_start(artifacts.start),
    )
    weekly_values = [float(item[1]) for item in weekly]
    psr_spec = document["development"]["statistical_confidence"]["psr"]
    if psr_spec["return_frequency"] != "weekly_utc":
        raise ValueError("unknown PSR return frequency")
    psr = probabilistic_sharpe_ratio(
        weekly_values,
        annualized_psr_benchmark(float(psr_spec["benchmark_sharpe"]), psr_spec["return_frequency"]),
    )

    metrics: dict[str, Any] = {
        "cagr": growth,
        "calmar": calmar_ratio(growth, mdd),
        "max_drawdown": mdd,
        "total_return": ret,
        "starting_equity": start_eq,
        "ending_equity": end_eq,
        "trade_count": len(trades),
        "trade_count_by_symbol": dict(counts),
        "profit_factor": pf,
        "psr": as_decimal(round(psr, 12)),
        "has_prior_version": artifacts.prior_version_id is not None,
        "mutation_count": int(artifacts.mutation_count),
        "window_count": 0,
        "window_max_drawdowns": (),
        "positive_window_fraction": ZERO,
        "concentration_symbol_max": ZERO,
        "concentration_direction_max": ZERO,
        "concentration_period_max": ZERO,
    }

    if kind == "oos":
        metrics["sealed_window_months"] = month_span(artifacts.start, artifacts.end)
        metrics["dsr"] = None
        metrics["profit_factor_stress"] = None
        metrics["profit_factor_bootstrap_lower_bound"] = None
        metrics["neighborhood_survival_fraction"] = None
        metrics["leave_one_out_surviving_count"] = None
        return metrics

    windows = walk_forward_windows(
        artifacts.start,
        artifacts.end,
        months=int(document["development"]["walk_forward"]["window_months"]),
    )
    if document["development"]["walk_forward"]["overlap"] is not False:
        raise ValueError("walk-forward overlap must be false")
    window_drawdowns: list[Decimal] = []
    positives = 0
    period_totals: dict[str, Decimal] = {}
    for index, (w_start, w_end) in enumerate(windows):
        eqs = window_equities(curve, w_start, w_end, artifacts.starting_equity)
        window_drawdowns.append(max_drawdown(eqs))
        w_ret = total_return(eqs[0], eqs[-1])
        if w_ret > 0:
            positives += 1
        bucket = [row for row in trades if in_half_open(row.exit_time, w_start, w_end)]
        period_totals[str(index)] = sum((row.net_pnl for row in bucket), ZERO)
    metrics["window_count"] = len(windows)
    metrics["window_max_drawdowns"] = tuple(window_drawdowns)
    if windows:
        metrics["positive_window_fraction"] = as_decimal(positives) / as_decimal(len(windows))

    stress = document["development"]["execution_stress"]
    metrics["profit_factor_stress"] = profit_factor(
        [stressed_net_pnl(trade, stress) for trade in trades]
    )
    boot = document["development"]["profit_factor"]["bootstrap"]
    metrics["profit_factor_bootstrap_lower_bound"] = trade_entry_moving_block_lower_bound(
        trades,
        block_days=int(boot["block_days"]),
        resamples=int(boot["resamples"]),
        seed=int(boot["seed"]),
        confidence=float(boot["confidence"]),
        algorithm=str(boot["algorithm"]),
        preserve=tuple(boot["preserve"]),
    )

    symbol_share = positive_contribution_share(group_net_pnl(trades, lambda row: row.symbol))
    direction_share = positive_contribution_share(group_net_pnl(trades, lambda row: row.direction))
    period_share = positive_contribution_share(period_totals)
    metrics["concentration_symbol_max"] = max(symbol_share.values(), default=ZERO)
    metrics["concentration_direction_max"] = max(direction_share.values(), default=ZERO)
    metrics["concentration_period_max"] = max(period_share.values(), default=ZERO)

    neighborhood = document["development"]["neighborhood"]
    if neighborhood["perturbation"] != "one_parameter_at_a_time_plus_minus_10_percent":
        raise ValueError("unknown neighborhood perturbation")
    params = artifacts.parameters or StrategyParameters()
    required = len(perturbed_parameters(params))
    n_cases = artifacts.neighborhood
    n_survive = sum(1 for case in n_cases if case_survives(case, neighborhood["survival"]))
    metrics["neighborhood_case_count"] = len(n_cases)
    metrics["neighborhood_required_cases"] = required
    if len(n_cases) != required:
        metrics["neighborhood_survival_fraction"] = ZERO
    else:
        metrics["neighborhood_survival_fraction"] = as_decimal(n_survive) / as_decimal(required)

    loo = document["development"]["leave_one_symbol_out"]
    loo_cases = artifacts.leave_one_out
    loo_survive = sum(1 for case in loo_cases if case_survives(case, loo["survival"]))
    metrics["leave_one_out_surviving_count"] = loo_survive
    metrics["leave_one_out_case_count"] = len(loo_cases)
    metrics["leave_one_out_required_cases"] = int(loo["cases"])

    dsr_spec = document["development"]["statistical_confidence"]["dsr"]
    daily = sampled_returns(
        curve,
        artifacts.start,
        artifacts.end,
        artifacts.starting_equity,
        timedelta(days=1),
        artifacts.start.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    trial_series = artifacts.trial_daily_returns or (tuple(daily),)
    dsr = deflated_sharpe_ratio(
        weekly_values,
        trial_count=trial_count,
        daily_series=trial_series,
        correlation_treatment=str(dsr_spec["correlation_treatment"]),
    )
    metrics["dsr"] = as_decimal(round(dsr, 12))
    metrics["trial_count"] = trial_count
    return metrics
