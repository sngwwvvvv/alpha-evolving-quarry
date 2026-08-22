"""Validation policy v2, deterministic metrics, and sealed OOS control."""

from trading_desk.validation.gates import (
    BudgetExhausted,
    EvaluationPolicy,
    GateResult,
    ResultBundle,
    TechnicalEvaluationError,
    evaluate_development,
)
from trading_desk.validation.metrics import (
    EquityPoint,
    EvaluationArtifacts,
    SurvivalCase,
    TradeRecord,
)
from trading_desk.validation.oos import (
    SealedOos,
    SealedOosError,
    evaluate_oos_once,
    research_inputs,
)

__all__ = [
    "BudgetExhausted",
    "EquityPoint",
    "EvaluationArtifacts",
    "EvaluationPolicy",
    "GateResult",
    "ResultBundle",
    "SealedOos",
    "SealedOosError",
    "SurvivalCase",
    "TechnicalEvaluationError",
    "TradeRecord",
    "evaluate_development",
    "evaluate_oos_once",
    "research_inputs",
]
