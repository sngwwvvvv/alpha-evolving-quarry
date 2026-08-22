"""Advisory agent envelopes and isolated Hermes profile adapters."""

from trading_desk.agents.capabilities import (
    CapabilityError,
    analysis_ledger_inputs,
    coding_inputs,
    orchestrator_inputs,
    research_inputs,
)
from trading_desk.agents.hermes import (
    AGENT_ERROR,
    FakeProfileAdapter,
    HermesAdapter,
    PROFILE_CONFIGS,
    ProfileAdapter,
    ProfileConfig,
    ProfileUnavailable,
    ProviderError,
    QualificationTaskResult,
    coding_qualification_gate,
    make_job,
    research_oauth_preflight,
)
from trading_desk.agents.schemas import AgentJob, AgentResult

__all__ = [
    "AGENT_ERROR",
    "AgentJob",
    "AgentResult",
    "CapabilityError",
    "FakeProfileAdapter",
    "HermesAdapter",
    "PROFILE_CONFIGS",
    "ProfileAdapter",
    "ProfileConfig",
    "ProfileUnavailable",
    "ProviderError",
    "QualificationTaskResult",
    "analysis_ledger_inputs",
    "coding_inputs",
    "coding_qualification_gate",
    "make_job",
    "orchestrator_inputs",
    "research_inputs",
    "research_oauth_preflight",
]
