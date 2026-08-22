from __future__ import annotations

import functools
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml

from trading_desk.config import SUPPORTED_SYMBOLS, canonical_json, sha256_hex
from trading_desk.state.db import BudgetKind, Database
from trading_desk.validation.metrics import EvaluationArtifacts, as_decimal, compute_metrics

POLICY_V2_PATH = Path(__file__).with_name("policy_v2.yaml")


class GateResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class TechnicalEvaluationError(Exception):
    """RUN_ERROR / DATA_BLOCKED; does not consume selection or OOS budget."""


class BudgetExhausted(Exception):
    """Family performance or OOS evaluation budget is exhausted."""


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
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return format(round(value, 12), "f")
    return value


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    document: dict[str, Any]
    policy_hash: str

    @property
    def version(self) -> str:
        return str(self.document["version"])

    @staticmethod
    def _jsonable(value: Any) -> Any:
        return _jsonable(value)

    @classmethod
    def v2(cls) -> EvaluationPolicy:
        return load_policy_v2()


@functools.lru_cache(maxsize=1)
def load_policy_v2() -> EvaluationPolicy:
    document = yaml.safe_load(POLICY_V2_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != "validation-policy-v2":
        raise ValueError("invalid validation policy")
    payload = _jsonable(document)
    return EvaluationPolicy(document=document, policy_hash=sha256_hex(canonical_json(payload)))


@dataclass(frozen=True, slots=True)
class ResultBundle:
    kind: str
    outcome: GateResult
    gates: dict[str, GateResult]
    metrics: dict[str, Any]
    policy_hash: str
    policy_version: str
    family_disposition: str | None = None
    consumed_budget: str | None = None
    bundle_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "gates", dict(self.gates))
        object.__setattr__(self, "metrics", dict(self.metrics))
        if not self.bundle_hash:
            object.__setattr__(self, "bundle_hash", sha256_hex(canonical_json(self.to_payload())))

    def to_payload(self) -> dict[str, Any]:
        return {
            "consumed_budget": self.consumed_budget,
            "family_disposition": self.family_disposition,
            "gates": {name: self.gates[name].value for name in sorted(self.gates)},
            "kind": self.kind,
            "metrics": _jsonable(self.metrics),
            "outcome": self.outcome.value,
            "policy_hash": self.policy_hash,
            "policy_version": self.policy_version,
        }


def _ge(value: object, bound: object) -> GateResult:
    return GateResult.PASS if as_decimal(value) >= as_decimal(bound) else GateResult.FAIL


def _gt(value: object, bound: object) -> GateResult:
    return GateResult.PASS if as_decimal(value) > as_decimal(bound) else GateResult.FAIL


def _le(value: object, bound: object) -> GateResult:
    return GateResult.PASS if as_decimal(value) <= as_decimal(bound) else GateResult.FAIL


def _lt(value: object, bound: object) -> GateResult:
    return GateResult.PASS if as_decimal(value) < as_decimal(bound) else GateResult.FAIL


def evaluate_gates(
    metrics: Mapping[str, Any],
    policy: EvaluationPolicy,
    *,
    kind: str,
) -> dict[str, GateResult]:
    document = policy.document
    if kind == "oos":
        oos = document["oos"]
        return {
            "trades": _ge(metrics.get("trade_count", 0), oos["trades"]["gate_minimum"]),
            "total_return": _gt(
                metrics.get("total_return", 0),
                oos["total_return"]["gate_exclusive_minimum"],
            ),
            "profit_factor": _ge(
                metrics.get("profit_factor", 0),
                oos["profit_factor"]["gate_minimum"],
            ),
            "max_drawdown": _lt(
                metrics.get("max_drawdown", 1),
                oos["max_drawdown"]["gate_exclusive_maximum"],
            ),
            "sealed_window": (
                GateResult.PASS
                if metrics.get("sealed_window_months") == oos["sealed_window_months"]
                else GateResult.FAIL
            ),
        }

    development = document["development"]
    mdd_limit = development["risk"]["max_drawdown"]["gate_exclusive_maximum"]
    window_dds = tuple(metrics.get("window_max_drawdowns") or ())
    symbol_counts = metrics.get("trade_count_by_symbol") or {}
    per_symbol = development["trades"]["per_symbol"]["gate_minimum"]
    symbol_ok = all(int(symbol_counts.get(symbol, 0)) >= int(per_symbol) for symbol in SUPPORTED_SYMBOLS)
    mutation_required = int(document["budget"]["mutations_per_successor_version"])
    if metrics.get("has_prior_version"):
        mutation_gate = (
            GateResult.PASS
            if int(metrics.get("mutation_count", 0)) == mutation_required
            else GateResult.FAIL
        )
    else:
        mutation_gate = GateResult.PASS
    loo_count = int(metrics.get("leave_one_out_surviving_count") or 0)
    if "leave_one_out_case_count" in metrics and int(metrics["leave_one_out_case_count"]) != int(
        development["leave_one_symbol_out"]["cases"]
    ):
        loo_gate = GateResult.FAIL
    else:
        loo_gate = _ge(loo_count, development["leave_one_symbol_out"]["gate_surviving_count_minimum"])
    window_mdd_gate = (
        GateResult.FAIL
        if not window_dds
        else (
            GateResult.PASS
            if all(as_decimal(item) < as_decimal(mdd_limit) for item in window_dds)
            else GateResult.FAIL
        )
    )
    return {
        "cagr": _ge(metrics.get("cagr", 0), development["profitability"]["cagr"]["gate_minimum"]),
        "calmar": _ge(
            metrics.get("calmar", 0),
            development["profitability"]["calmar"]["gate_minimum"],
        ),
        "max_drawdown_aggregate": _lt(metrics.get("max_drawdown", 1), mdd_limit),
        "max_drawdown_windows": window_mdd_gate,
        "positive_window_fraction": _ge(
            metrics.get("positive_window_fraction", 0),
            development["walk_forward"]["positive_window_fraction"]["gate_minimum"],
        ),
        "trades_aggregate": _ge(
            metrics.get("trade_count", 0),
            development["trades"]["aggregate"]["gate_minimum"],
        ),
        "trades_per_symbol": GateResult.PASS if symbol_ok else GateResult.FAIL,
        "profit_factor_base": _ge(
            metrics.get("profit_factor", 0),
            development["profit_factor"]["base_point_estimate"]["gate_minimum"],
        ),
        "profit_factor_stress": _ge(
            metrics.get("profit_factor_stress", 0),
            development["profit_factor"]["stress_point_estimate"]["gate_minimum"],
        ),
        "profit_factor_bootstrap": _gt(
            metrics.get("profit_factor_bootstrap_lower_bound", 0),
            development["profit_factor"]["bootstrap"]["gate_lower_bound_exclusive_minimum"],
        ),
        "psr": _ge(
            metrics.get("psr", 0),
            development["statistical_confidence"]["psr"]["gate_probability_minimum"],
        ),
        "dsr": _ge(
            metrics.get("dsr", 0),
            development["statistical_confidence"]["dsr"]["gate_probability_minimum"],
        ),
        "concentration_symbol": _le(
            metrics.get("concentration_symbol_max", 1),
            development["concentration"]["symbol"]["gate_share_maximum"],
        ),
        "concentration_period": _le(
            metrics.get("concentration_period_max", 1),
            development["concentration"]["period"]["gate_share_maximum"],
        ),
        "neighborhood": (
            GateResult.FAIL
            if "neighborhood_case_count" in metrics
            and "neighborhood_required_cases" in metrics
            and int(metrics["neighborhood_case_count"]) != int(metrics["neighborhood_required_cases"])
            else _ge(
                metrics.get("neighborhood_survival_fraction", 0),
                development["neighborhood"]["gate_survival_fraction_minimum"],
            )
        ),
        "leave_one_symbol_out": loo_gate,
        "mutations_per_successor": mutation_gate,
    }


def _outcome(gates: Mapping[str, GateResult]) -> GateResult:
    return GateResult.PASS if all(item is GateResult.PASS for item in gates.values()) else GateResult.FAIL


def _consume(db: Database, family_id: str, kind: BudgetKind) -> str:
    try:
        with db.transaction() as conn:
            db.consume_budget(conn, family_id=family_id, kind=kind)
    except sqlite3.IntegrityError as exc:
        raise BudgetExhausted(kind) from exc
    return kind


def evaluate_development(
    artifacts: EvaluationArtifacts,
    policy: EvaluationPolicy | None = None,
    *,
    db: Database | None = None,
    family_id: str | None = None,
) -> ResultBundle:
    policy = policy or EvaluationPolicy.v2()
    if artifacts.technical_error:
        raise TechnicalEvaluationError(artifacts.technical_kind or "RUN_ERROR")
    family_id = family_id or artifacts.family_id
    trial_count = artifacts.performance_evaluated_versions
    if db is not None and family_id is not None:
        budget = db.get_budget(family_id)
        if budget.performance_evaluated_versions >= budget.max_performance_evaluated_versions:
            raise BudgetExhausted("performance")
        trial_count = budget.performance_evaluated_versions + 1
    metrics = compute_metrics(artifacts, policy, kind="development", trial_count=trial_count)
    gates = evaluate_gates(metrics, policy, kind="development")
    consumed = None
    if db is not None and family_id is not None:
        consumed = _consume(db, family_id, "performance")
    return ResultBundle(
        kind="development",
        outcome=_outcome(gates),
        gates=gates,
        metrics=metrics,
        policy_hash=policy.policy_hash,
        policy_version=policy.version,
        consumed_budget=consumed,
    )
