from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_desk.config import (
    SUPPORTED_SYMBOLS,
    Settings,
    canonical_json,
    sha256_hex,
    utc_now,
)


def test_supported_symbols_are_fixed() -> None:
    assert SUPPORTED_SYMBOLS == ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT")


def test_default_settings_are_immutable_and_utc() -> None:
    settings = Settings()
    assert settings.symbols == SUPPORTED_SYMBOLS
    assert settings.timezone is timezone.utc
    assert settings.artifact_root == Path("artifacts")
    assert settings.sqlite_path == Path("state") / "trading_desk.sqlite3"
    assert settings.policy_version == "validation-policy-v2"
    with pytest.raises(AttributeError):
        settings.policy_version = "other"  # type: ignore[misc]


def test_settings_reject_unsupported_symbols() -> None:
    with pytest.raises(ValueError, match="unsupported symbol"):
        Settings(symbols=("BTCUSDT", "DOGEUSDT"))


def test_canonical_json_and_hash_are_stable() -> None:
    settings = Settings()
    payload = {
        "symbols": list(settings.symbols),
        "timezone": str(settings.timezone),
        "artifact_root": settings.artifact_root.as_posix(),
        "sqlite_path": settings.sqlite_path.as_posix(),
        "policy_version": settings.policy_version,
    }
    first = canonical_json(payload)
    second = canonical_json(payload)
    assert first == second
    assert first == (
        '{"artifact_root":"artifacts","policy_version":"validation-policy-v2",'
        '"sqlite_path":"state/trading_desk.sqlite3",'
        '"symbols":["BTCUSDT","ETHUSDT","XRPUSDT","SOLUSDT"],'
        '"timezone":"UTC"}'
    )
    assert sha256_hex(first) == sha256_hex(second)
    assert len(sha256_hex(first)) == 64


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is timezone.utc
