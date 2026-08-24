from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from trading_desk.agents.capabilities import CapabilityError, reject_credentials, scan_bundle
from trading_desk.agents.schemas import (
    AGENT_ERROR,
    AgentJob,
    AgentResult,
    OK,
    SchemaError,
    validate_agent_response,
    validate_job_envelope,
)
from trading_desk.config import canonical_json, sha256_hex

Invoke = Callable[[AgentJob], Any]
Commit = Callable[[AgentResult], None]


class ProfileUnavailable(ValueError):
    """Pinned profile cannot be used until preflight or qualification passes."""


class ProviderError(Exception):
    """Pinned provider failed. No model fallback is attempted."""


class ProfileAdapter(Protocol):
    def submit(self, job: AgentJob) -> AgentResult:
        """Submit one pinned job envelope and return a validated result."""


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    name: str
    logical_model: str
    provider: str
    credential_env: str | None = None
    auth: str = "api_key"
    thinking: bool = False
    reasoning_effort: str | None = None
    fallback: bool = False


@dataclass(frozen=True, slots=True)
class QualificationTaskResult:
    requested_change_only: bool
    invariants_unchanged: bool
    tests_passing: bool
    schema_valid: bool
    no_unapproved_dependency: bool

    @property
    def accepted(self) -> bool:
        return (
            self.requested_change_only
            and self.invariants_unchanged
            and self.tests_passing
            and self.schema_valid
            and self.no_unapproved_dependency
        )


PROFILE_CONFIGS: dict[str, ProfileConfig] = {
    "orchestrator": ProfileConfig(
        name="orchestrator",
        logical_model="deepseek/deepseek-v4-pro-0813",
        provider="openrouter",
        credential_env="OPENROUTER_API_KEY",
        thinking=True,
        reasoning_effort="low",
    ),
    "research": ProfileConfig(
        name="research",
        logical_model="deepseek/deepseek-v4-pro-0813",
        provider="openrouter",
        credential_env="OPENROUTER_API_KEY",
    ),
    "coding": ProfileConfig(
        name="coding",
        logical_model="deepseek/deepseek-v4-flash-0731",
        provider="openrouter",
        credential_env="OPENROUTER_API_KEY",
    ),
    "analysis-ledger": ProfileConfig(
        name="analysis-ledger",
        logical_model="deepseek/deepseek-v4-flash-0731",
        provider="openrouter",
        credential_env="OPENROUTER_API_KEY",
        thinking=True,
    ),
}


def model_matches(logical: str, resolved: str) -> bool:
    if not resolved:
        return False
    logical_l = logical.lower()
    resolved_l = resolved.lower()
    return (
        resolved_l == logical_l
        or resolved_l.startswith(logical_l + "-")
        or resolved_l.startswith(logical_l + "/")
    )


def resolved_fingerprint(profile: str, resolved_model_id: str) -> str:
    spec = PROFILE_CONFIGS[profile]
    return sha256_hex(
        canonical_json(
            {
                "logical_model": spec.logical_model,
                "provider": spec.provider,
                "reasoning_effort": spec.reasoning_effort,
                "resolved_model_id": resolved_model_id,
                "thinking": spec.thinking,
            }
        )
    )


def make_job(
    profile: str,
    action: str,
    input_bundle: Mapping[str, Any],
    *,
    logical_model: str | None = None,
    provider: str | None = None,
    artifact_refs: Sequence[str] = (),
    job_id: str | None = None,
) -> AgentJob:
    try:
        spec = PROFILE_CONFIGS[profile]
    except KeyError as exc:
        raise ValueError(f"unknown profile: {profile}") from exc
    if spec.fallback:
        raise ValueError("profile fallback is not allowed")
    if logical_model is not None and logical_model != spec.logical_model:
        raise ValueError("no model fallback; job must use the pinned logical model")
    if provider is not None and provider != spec.provider:
        raise ValueError("no provider fallback")
    bundle = dict(input_bundle)
    scan_bundle(profile, bundle)
    worktree = bundle.pop("worktree", None) if profile == "coding" else None
    if profile == "coding":
        if not isinstance(worktree, Mapping) or worktree.get("disposable") is not True:
            raise ValueError("coding requires disposable worktree metadata")
        worktree = dict(worktree)
    elif "worktree" in input_bundle:
        raise ValueError("worktree is only valid for coding")
    job = AgentJob(
        job_id=job_id or uuid.uuid4().hex,
        profile=profile,
        action=action,
        input_bundle=bundle,
        logical_model=spec.logical_model,
        provider=spec.provider,
        artifact_refs=tuple(artifact_refs),
        worktree=worktree,
    )
    envelope = job.to_envelope()
    validate_job_envelope(envelope)
    scan_bundle(profile, envelope)
    return job


def _unconfigured(_job: AgentJob) -> Any:
    raise ProviderError("Hermes transport is not configured")


def _parse_raw(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(raw, dict):
        return raw
    return None


def _interpret(job: AgentJob, raw: Any) -> tuple[dict[str, Any] | None, str]:
    parsed = _parse_raw(raw)
    if parsed is None:
        return None, "invalid JSON"
    try:
        validate_agent_response(job.profile, parsed, input_bundle=job.input_bundle)
    except (SchemaError, KeyError) as exc:
        return None, str(exc)
    if parsed["job_id"] != job.job_id or parsed["pin"] != job.pin:
        return None, "pinned job mismatch"
    if parsed["status"] != OK:
        return None, "agent error"
    if not model_matches(job.logical_model, str(parsed["resolved_model_id"])):
        return None, "model fallback forbidden"
    payload = parsed["payload"]
    if job.profile == "coding" and payload.get("added_dependencies") and not job.input_bundle.get(
        "allow_dependencies"
    ):
        return None, "unapproved dependency"
    if job.profile == "coding" and payload.get("test_results", {}).get("passed") is not True:
        return None, "coding tests did not pass"
    try:
        reject_credentials(parsed)
    except CapabilityError as exc:
        return None, str(exc)
    return parsed, ""


def _error_result(job: AgentJob, *, attempts: int, reason: str) -> AgentResult:
    return AgentResult(
        job_id=job.job_id,
        status=AGENT_ERROR,
        pin=job.pin,
        attempts=attempts,
        payload={},
        reason=reason,
        profile=job.profile,
        logical_model=job.logical_model,
        provider=job.provider,
    )


class HermesAdapter:
    """Typed Hermes profile adapter. Transport is injected; the default never networks."""

    def __init__(
        self,
        invoke: Invoke | None = None,
        *,
        on_success: Commit | None = None,
    ) -> None:
        self._invoke = invoke or _unconfigured
        self._on_success = on_success

    def submit(self, job: AgentJob) -> AgentResult:
        envelope = job.to_envelope()
        validate_job_envelope(envelope)
        scan_bundle(job.profile, job.input_bundle)
        if job.worktree is not None:
            scan_bundle(job.profile, job.worktree)
        scan_bundle(job.profile, envelope)
        pin = job.pin
        last_reason = "invalid agent output"
        for attempt in (1, 2):
            if job.pin != pin:
                return _error_result(job, attempts=attempt, reason="pinned job mutated")
            try:
                raw = self._invoke(job)
            except ProviderError as exc:
                return _error_result(job, attempts=attempt, reason=str(exc))
            parsed, reason = _interpret(job, raw)
            if parsed is None:
                last_reason = reason
                continue
            result = AgentResult(
                job_id=job.job_id,
                status=OK,
                pin=pin,
                attempts=attempt,
                payload=dict(parsed["payload"]),
                artifacts=tuple(dict(item) for item in parsed["artifacts"]),
                resolved_model_id=str(parsed["resolved_model_id"]),
                resolved_fingerprint=resolved_fingerprint(job.profile, str(parsed["resolved_model_id"])),
                profile=job.profile,
                logical_model=job.logical_model,
                provider=job.provider,
            )
            reject_credentials(result.to_payload())
            if self._on_success is not None:
                self._on_success(result)
            return result
        return _error_result(job, attempts=2, reason=last_reason)


class FakeProfileAdapter(HermesAdapter):
    """In-process stub. Never contacts Hermes or a network."""

    def __init__(self, outputs: Sequence[Any], *, on_success: Commit | None = None) -> None:
        self._outputs = list(outputs)
        self.jobs: list[AgentJob] = []
        super().__init__(invoke=self._invoke, on_success=on_success)

    def _invoke(self, job: AgentJob) -> Any:
        self.jobs.append(job)
        if not self._outputs:
            raise ProviderError("fake adapter exhausted")
        item = self._outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(job)
        return item


def research_preflight(resolve: Callable[[str, str], str | None]) -> str:
    spec = PROFILE_CONFIGS["research"]
    resolved = resolve(spec.logical_model, spec.provider)
    if not resolved or not model_matches(spec.logical_model, resolved):
        raise ProfileUnavailable(
            "research profile unavailable: deepseek/deepseek-v4-pro-0813 did not resolve through openrouter"
        )
    return resolved


def coding_qualification_gate(results: Sequence[QualificationTaskResult]) -> bool:
    if len(results) < 10:
        raise ProfileUnavailable("coding profile requires 10 qualification tasks")
    accepted = sum(1 for item in results if item.accepted)
    if accepted * 10 < 9 * len(results):
        raise ProfileUnavailable("coding profile acceptance below 90%")
    return True
