from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from trading_desk.data.contracts import DerivedBar
from trading_desk.strategy.models import (
    BEAR,
    BULL,
    DEFAULT_FAMILY_ID,
    LONG,
    NEUTRAL,
    SHORT,
    StrategyFamily,
    StrategyParameters,
    StrategySignal,
)

DEFAULT_FAMILY = StrategyFamily(
    family_id=DEFAULT_FAMILY_ID,
    topology="hourly-ema-vs-close",
    feature_set=("hourly_close", "hourly_ema", "daily_ema_50", "daily_ema_200"),
    entry_exit="regime-filtered-ema-stop-target",
    lifecycle="single-position-tp-sl",
)


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period < 1 or len(values) < period:
        return None
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    multiplier = Decimal(2) / Decimal(period + 1)
    value = seed
    one = Decimal(1)
    for price in values[period:]:
        value = price * multiplier + value * (one - multiplier)
    return value


def regime(daily_closes: Sequence[Decimal]) -> str:
    ema_50 = ema(daily_closes, 50)
    ema_200 = ema(daily_closes, 200)
    if ema_50 is None or ema_200 is None:
        return NEUTRAL
    if ema_50 > ema_200:
        return BULL
    if ema_50 < ema_200:
        return BEAR
    return NEUTRAL


def _levels(close: Decimal, direction: str, parameters: StrategyParameters) -> tuple[Decimal, Decimal]:
    distance = close * parameters.stop_pct
    target = distance * parameters.take_profit_r
    if direction == LONG:
        return close - distance, close + target
    return close + distance, close - target


class DefaultStrategy:
    def __init__(self, parameters: StrategyParameters | None = None) -> None:
        self.parameters = parameters or StrategyParameters()
        self.family = DEFAULT_FAMILY

    def regime(self, daily_closes: Sequence[Decimal]) -> str:
        return regime(daily_closes)

    def signal(
        self,
        symbol: str,
        completed_hour: DerivedBar,
        hourly_closes: Sequence[Decimal],
        daily_closes: Sequence[Decimal],
    ) -> StrategySignal | None:
        lookback = self.parameters.hourly_ema_lookback
        if len(hourly_closes) < lookback:
            return None
        market = regime(daily_closes)
        basis = ema(hourly_closes, lookback)
        close = hourly_closes[-1]
        if basis is None or close == basis:
            return None
        if close > basis and market == BULL:
            direction = LONG
        elif close < basis and market == BEAR:
            direction = SHORT
        else:
            return None
        stop, take_profit = _levels(close, direction, self.parameters)
        return StrategySignal(
            symbol=symbol,
            direction=direction,
            bar_open_time=completed_hour.open_time,
            published_at=completed_hour.open_time + timedelta(hours=1),
            close=close,
            stop=stop,
            take_profit=take_profit,
        )
