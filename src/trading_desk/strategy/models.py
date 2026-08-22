from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from trading_desk.config import SUPPORTED_SYMBOLS, canonical_json, sha256_hex

OK = "OK"
MDD_FAIL = "MDD_FAIL"
LONG = "LONG"
SHORT = "SHORT"
BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"
MARGIN_MODE = "isolated"
SYSTEM_LEVERAGE = Decimal("2")
GROSS_LEVERAGE_CEILING = Decimal("2")
PER_POSITION_RISK = Decimal("0.005")
AGGREGATE_PLANNED_RISK = Decimal("0.02")
DAILY_LOSS_STOP = Decimal("0.02")
MDD_HALT = Decimal("0.15")
DEFAULT_FAMILY_ID = "default-hourly-ema-regime"
DEFAULT_FEE_RATE = Decimal("0.0004")
DEFAULT_SLIPPAGE_RATE = Decimal("0.0005")

_PARAMETER_FIELDS = ("hourly_ema_lookback", "stop_pct", "take_profit_r")


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    hourly_ema_lookback: int = 20
    stop_pct: Decimal = Decimal("0.015")
    take_profit_r: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_pct", Decimal(str(self.stop_pct)))
        object.__setattr__(self, "take_profit_r", Decimal(str(self.take_profit_r)))
        if int(self.hourly_ema_lookback) != self.hourly_ema_lookback or self.hourly_ema_lookback < 1:
            raise ValueError("hourly_ema_lookback must be a positive integer")
        object.__setattr__(self, "hourly_ema_lookback", int(self.hourly_ema_lookback))
        if self.stop_pct <= 0 or self.take_profit_r <= 0:
            raise ValueError("stop_pct and take_profit_r must be positive")

    def as_mapping(self) -> dict[str, str | int]:
        return {
            "hourly_ema_lookback": self.hourly_ema_lookback,
            "stop_pct": format(self.stop_pct, "f"),
            "take_profit_r": format(self.take_profit_r, "f"),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> StrategyParameters:
        keys = set(data)
        if keys & set(SUPPORTED_SYMBOLS):
            raise ValueError("asset-specific parameter")
        unknown = keys - set(_PARAMETER_FIELDS)
        if unknown:
            raise ValueError(f"unknown parameter: {', '.join(sorted(unknown))}")
        return cls(
            hourly_ema_lookback=int(data["hourly_ema_lookback"]),
            stop_pct=Decimal(str(data["stop_pct"])),
            take_profit_r=Decimal(str(data["take_profit_r"])),
        )


@dataclass(frozen=True, slots=True)
class StrategyFamily:
    family_id: str
    topology: str
    feature_set: tuple[str, ...]
    entry_exit: str
    lifecycle: str


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    family_id: str
    strategy_version_id: str
    code_commit: str
    parameters: StrategyParameters
    spec_hash: str


@dataclass(frozen=True, slots=True)
class StrategySignal:
    symbol: str
    direction: str
    bar_open_time: datetime
    published_at: datetime
    close: Decimal
    stop: Decimal
    take_profit: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    fee_rate: Decimal = DEFAULT_FEE_RATE
    slippage_rate: Decimal = DEFAULT_SLIPPAGE_RATE
    policy_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "fee_rate", Decimal(str(self.fee_rate)))
        object.__setattr__(self, "slippage_rate", Decimal(str(self.slippage_rate)))
        if self.fee_rate < 0 or self.slippage_rate < 0:
            raise ValueError("fee_rate and slippage_rate must be non-negative")
        payload = {
            "fee_rate": format(self.fee_rate, "f"),
            "slippage_rate": format(self.slippage_rate, "f"),
        }
        object.__setattr__(self, "policy_hash", sha256_hex(canonical_json(payload)))


@dataclass(frozen=True, slots=True)
class Position:
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


def make_strategy_version(
    *,
    code_commit: str,
    parameters: StrategyParameters | None = None,
    family_id: str = DEFAULT_FAMILY_ID,
) -> StrategyVersion:
    params = parameters or StrategyParameters()
    spec = params.as_mapping()
    spec_hash = sha256_hex(canonical_json(spec))
    version_id = sha256_hex(
        canonical_json({"code_commit": code_commit, "family_id": family_id, "spec": spec})
    )
    return StrategyVersion(
        family_id=family_id,
        strategy_version_id=version_id,
        code_commit=code_commit,
        parameters=params,
        spec_hash=spec_hash,
    )
