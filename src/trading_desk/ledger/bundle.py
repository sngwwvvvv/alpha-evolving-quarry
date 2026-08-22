from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from trading_desk.config import canonical_json, sha256_hex
from trading_desk.validation.gates import GateResult, ResultBundle
from trading_desk.validation.metrics import TradeRecord, as_decimal
from trading_desk.validation.walk_forward import in_half_open, walk_forward_windows

ZERO = Decimal("0")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, GateResult):
        return value.value
    if isinstance(value, Decimal):
        if value.is_infinite():
            return "inf" if value > 0 else "-inf"
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def trade_payload(trade: TradeRecord) -> dict[str, str]:
    return {
        "direction": trade.direction,
        "entry_time": trade.entry_time.isoformat(),
        "exit_reason": trade.exit_reason,
        "exit_time": trade.exit_time.isoformat(),
        "fees": format(as_decimal(trade.fees), "f"),
        "funding": format(as_decimal(trade.funding), "f"),
        "net_pnl": format(as_decimal(trade.net_pnl), "f"),
        "regime": trade.regime,
        "slippage_cost": format(as_decimal(trade.slippage_cost), "f"),
        "symbol": trade.symbol,
    }


def trade_reference(trade: TradeRecord) -> str:
    return sha256_hex(canonical_json(trade_payload(trade)))


@dataclass(frozen=True, slots=True)
class MutationManifest:
    prior_version_id: str
    hypothesis: str
    files_changed: tuple[str, ...]
    configuration_fields_changed: tuple[str, ...]
    expected_causal_effect: str
    invariant_diff_result: str
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "files_changed", tuple(self.files_changed))
        object.__setattr__(self, "configuration_fields_changed", tuple(self.configuration_fields_changed))
        if not self.manifest_hash:
            object.__setattr__(self, "manifest_hash", sha256_hex(canonical_json(self.to_payload())))

    def to_payload(self) -> dict[str, Any]:
        return {
            "configuration_fields_changed": list(self.configuration_fields_changed),
            "expected_causal_effect": self.expected_causal_effect,
            "files_changed": list(self.files_changed),
            "hypothesis": self.hypothesis,
            "invariant_diff_result": self.invariant_diff_result,
            "prior_version_id": self.prior_version_id,
        }


@dataclass(frozen=True, slots=True)
class LedgerBundle:
    kind: str
    outcome: str
    executive_summary: str
    gates_failed: tuple[str, ...]
    gates_achieved: tuple[str, ...]
    loss_attribution: dict[str, Any]
    run_id: str
    version_id: str
    trade_references: tuple[str, ...]
    result_bundle_hash: str
    prior_version_comparison: dict[str, Any] | None = None
    mutation_hypothesis: str | None = None
    mutation: MutationManifest | None = None
    bundle_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "gates_failed", tuple(self.gates_failed))
        object.__setattr__(self, "gates_achieved", tuple(self.gates_achieved))
        object.__setattr__(self, "trade_references", tuple(self.trade_references))
        object.__setattr__(self, "loss_attribution", dict(self.loss_attribution))
        if self.prior_version_comparison is not None:
            object.__setattr__(self, "prior_version_comparison", dict(self.prior_version_comparison))
        if not self.bundle_hash:
            object.__setattr__(self, "bundle_hash", sha256_hex(canonical_json(self.to_payload())))

    def to_payload(self) -> dict[str, Any]:
        return {
            "executive_summary": self.executive_summary,
            "gates_achieved": list(self.gates_achieved),
            "gates_failed": list(self.gates_failed),
            "kind": self.kind,
            "loss_attribution": _jsonable(self.loss_attribution),
            "mutation": None if self.mutation is None else self.mutation.to_payload(),
            "mutation_hypothesis": self.mutation_hypothesis,
            "outcome": self.outcome,
            "prior_version_comparison": _jsonable(self.prior_version_comparison),
            "result_bundle_hash": self.result_bundle_hash,
            "run_id": self.run_id,
            "trade_references": list(self.trade_references),
            "version_id": self.version_id,
        }


def _sum_losses(trades: Sequence[TradeRecord], key_fn) -> dict[str, str]:
    totals: dict[str, Decimal] = {}
    for trade in trades:
        key = str(key_fn(trade) or "unknown")
        loss = min(as_decimal(trade.net_pnl), ZERO)
        totals[key] = totals.get(key, ZERO) + loss
    return {key: format(value, "f") for key, value in sorted(totals.items())}


def _cost_types(trades: Sequence[TradeRecord]) -> dict[str, str]:
    fees = sum((as_decimal(row.fees) for row in trades), ZERO)
    paid_funding = sum((as_decimal(row.funding) for row in trades if as_decimal(row.funding) < 0), ZERO)
    slippage = sum((as_decimal(row.slippage_cost) for row in trades), ZERO)
    return {
        "fees": format(fees, "f"),
        "funding": format(paid_funding, "f"),
        "slippage": format(slippage, "f"),
    }


def attribute_losses(
    trades: Sequence[TradeRecord],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    windows = walk_forward_windows(start, end, months=6) if end > start else ()

    def period_key(trade: TradeRecord) -> str:
        for index, (left, right) in enumerate(windows):
            if in_half_open(trade.exit_time, left, right):
                return str(index)
        return "unknown"

    return {
        "by_cost_type": _cost_types(trades),
        "by_direction": _sum_losses(trades, lambda row: row.direction),
        "by_exit_reason": _sum_losses(trades, lambda row: row.exit_reason or "unknown"),
        "by_period": _sum_losses(trades, period_key),
        "by_regime": _sum_losses(trades, lambda row: row.regime or "unknown"),
        "by_symbol": _sum_losses(trades, lambda row: row.symbol),
    }


def _fmt_decimal(value: Decimal) -> str:
    if value.is_infinite():
        return "inf" if value > 0 else "-inf"
    return format(value, "f")


def _safe_delta(left: Decimal, right: Decimal) -> Decimal:
    try:
        return left - right
    except InvalidOperation:
        if left.is_infinite() and right.is_infinite() and (left > 0) == (right > 0):
            return ZERO
        if left.is_infinite():
            return left
        if right.is_infinite():
            return -right
        raise


def _metric_delta(current: Mapping[str, Any], prior: Mapping[str, Any], key: str) -> dict[str, str] | None:
    if key not in current or key not in prior:
        return None
    left = as_decimal(current[key])
    right = as_decimal(prior[key])
    return {
        "current": _fmt_decimal(left),
        "delta": _fmt_decimal(_safe_delta(left, right)),
        "prior": _fmt_decimal(right),
    }


def compare_versions(
    current: ResultBundle,
    prior: ResultBundle,
    *,
    prior_version_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prior_outcome": prior.outcome.value,
        "prior_version_id": prior_version_id,
    }
    for key in ("cagr", "max_drawdown", "profit_factor", "total_return", "trade_count"):
        delta = _metric_delta(current.metrics, prior.metrics, key)
        if delta is not None:
            payload[key] = delta
    return payload


def build_ledger_bundle(
    *,
    kind: str,
    result: ResultBundle,
    run_id: str,
    version_id: str,
    trades: Sequence[TradeRecord],
    start: datetime,
    end: datetime,
    prior_result: ResultBundle | None = None,
    prior_version_id: str | None = None,
    mutation_hypothesis: str | None = None,
    mutation: MutationManifest | None = None,
) -> LedgerBundle:
    if kind == "oos" and (mutation is not None or mutation_hypothesis):
        raise ValueError("OOS failure analysis must not contain a mutation proposal")
    failed = tuple(name for name, gate in result.gates.items() if gate is GateResult.FAIL)
    achieved = tuple(name for name, gate in result.gates.items() if gate is GateResult.PASS)
    comparison = None
    if prior_result is not None and prior_version_id is not None:
        comparison = compare_versions(result, prior_result, prior_version_id=prior_version_id)
    hypothesis = mutation_hypothesis
    if kind != "development" or result.outcome is not GateResult.FAIL:
        hypothesis = None
        mutation = None
    elif hypothesis is None and failed:
        hypothesis = "Address failed gates: " + ", ".join(failed)
    return LedgerBundle(
        kind=kind,
        outcome=result.outcome.value,
        executive_summary=(
            f"{kind} {result.outcome.value}: {len(failed)} failed gates, {len(achieved)} achieved"
        ),
        gates_failed=failed,
        gates_achieved=achieved,
        loss_attribution=attribute_losses(trades, start=start, end=end),
        run_id=run_id,
        version_id=version_id,
        trade_references=tuple(trade_reference(row) for row in trades),
        result_bundle_hash=result.bundle_hash,
        prior_version_comparison=comparison,
        mutation_hypothesis=hypothesis,
        mutation=mutation,
    )
