from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT")

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    unsupported = [symbol for symbol in symbols if symbol not in SUPPORTED_SYMBOLS]
    if unsupported:
        raise ValueError(f"unsupported symbol: {', '.join(unsupported)}")
    if tuple(symbols) != SUPPORTED_SYMBOLS:
        raise ValueError("unsupported symbol: settings require exactly the four supported symbols")
    return SUPPORTED_SYMBOLS


@dataclass(frozen=True, slots=True)
class Settings:
    symbols: tuple[str, ...] = field(default=SUPPORTED_SYMBOLS)
    timezone: timezone = field(default=UTC)
    artifact_root: Path = field(default_factory=lambda: Path("artifacts"))
    sqlite_path: Path = field(default_factory=lambda: Path("state") / "trading_desk.sqlite3")
    policy_version: str = "validation-policy-v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _validate_symbols(tuple(self.symbols)))
        if self.timezone is not UTC:
            raise ValueError("timezone must be UTC")
        object.__setattr__(self, "artifact_root", Path(self.artifact_root))
        object.__setattr__(self, "sqlite_path", Path(self.sqlite_path))
