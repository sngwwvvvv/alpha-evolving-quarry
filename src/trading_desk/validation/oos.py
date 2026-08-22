from __future__ import annotations

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


def research_inputs(*parts: Any, **kwargs: Any) -> dict[str, Any]:
    from trading_desk.agents.capabilities import research_inputs as sealed_research_inputs

    return sealed_research_inputs(*parts, **kwargs)


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
