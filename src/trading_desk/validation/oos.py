from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_desk.state.db import Database
from trading_desk.validation.gates import (
    BudgetExhausted,
    EvaluationPolicy,
    GateResult,
    ResultBundle,
    TechnicalEvaluationError,
    _consume,
    _outcome,
    evaluate_gates,
)
from trading_desk.validation.metrics import EvaluationArtifacts, compute_metrics


class SealedOosError(Exception):
    """Family already consumed its single sealed OOS evaluation."""


class SealedOos:
    def __init__(
        self,
        artifacts: EvaluationArtifacts,
        *,
        path: str | None = None,
        credentials: str | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._path = path
        self._credentials = credentials

    def artifacts_for_authorized_run(self) -> EvaluationArtifacts:
        return self._artifacts

    def __repr__(self) -> str:
        return "SealedOos(sealed=True)"


def _is_oos_key(key: str) -> bool:
    lowered = key.lower()
    return "oos" in lowered


def research_inputs(*parts: Any, **kwargs: Any) -> dict[str, Any]:
    if kwargs.get("oos") is not None:
        raise ValueError("OOS artifacts cannot enter research inputs")
    payload: dict[str, Any] = {}

    def _reject_mapping(data: Mapping[str, Any]) -> None:
        for key in data:
            if _is_oos_key(str(key)):
                raise ValueError("OOS artifacts cannot enter research inputs")

    for part in parts:
        if isinstance(part, ResultBundle) and part.kind == "oos":
            raise ValueError("OOS artifacts cannot enter research inputs")
        if isinstance(part, SealedOos):
            raise ValueError("OOS artifacts cannot enter research inputs")
        if isinstance(part, Mapping):
            _reject_mapping(part)
            payload.update(dict(part))
        elif isinstance(part, ResultBundle):
            payload["development"] = part
        elif part is not None:
            raise ValueError("unsupported research input")
    for key, value in kwargs.items():
        if key == "oos":
            continue
        if _is_oos_key(key):
            raise ValueError("OOS artifacts cannot enter research inputs")
        if isinstance(value, ResultBundle) and value.kind == "oos":
            raise ValueError("OOS artifacts cannot enter research inputs")
        if isinstance(value, SealedOos):
            raise ValueError("OOS artifacts cannot enter research inputs")
        if isinstance(value, Mapping):
            _reject_mapping(value)
        if value is not None:
            payload[key] = value
    return payload


def evaluate_oos_once(
    source: SealedOos | EvaluationArtifacts,
    policy: EvaluationPolicy | None = None,
    *,
    db: Database | None = None,
    family_id: str | None = None,
) -> ResultBundle:
    policy = policy or EvaluationPolicy.v2()
    artifacts = (
        source.artifacts_for_authorized_run() if isinstance(source, SealedOos) else source
    )
    if artifacts.technical_error:
        raise TechnicalEvaluationError(artifacts.technical_kind or "RUN_ERROR")
    family_id = family_id or artifacts.family_id
    if db is not None and family_id is not None:
        budget = db.get_budget(family_id)
        if budget.oos_evaluations >= budget.max_oos_evaluations:
            raise SealedOosError("family already used sealed OOS")
        if budget.oos_evaluations >= int(policy.document["oos"]["evaluations_per_family"]):
            raise SealedOosError("family already used sealed OOS")
    metrics = compute_metrics(artifacts, policy, kind="oos", trial_count=1)
    gates = evaluate_gates(metrics, policy, kind="oos")
    outcome = _outcome(gates)
    consumed = None
    if db is not None and family_id is not None:
        try:
            consumed = _consume(db, family_id, "oos")
        except BudgetExhausted as exc:
            raise SealedOosError("family already used sealed OOS") from exc
    return ResultBundle(
        kind="oos",
        outcome=outcome,
        gates=gates,
        metrics=metrics,
        policy_hash=policy.policy_hash,
        policy_version=policy.version,
        family_disposition="READY_FOR_PAPER" if outcome is GateResult.PASS else "REJECTED",
        consumed_budget=consumed,
    )
