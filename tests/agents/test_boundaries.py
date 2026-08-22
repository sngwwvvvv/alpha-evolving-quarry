from __future__ import annotations

import ast
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_desk.agents import (
    AGENT_ERROR,
    CapabilityError,
    FakeProfileAdapter,
    PROFILE_CONFIGS,
    ProfileUnavailable,
    ProviderError,
    QualificationTaskResult,
    analysis_ledger_inputs,
    coding_inputs,
    coding_qualification_gate,
    make_job,
    orchestrator_inputs,
    research_inputs,
    research_oauth_preflight,
)
from trading_desk.config import UTC, canonical_json, sha256_hex
from trading_desk.state.db import Database
from trading_desk.storage.artifacts import ArtifactStore
from trading_desk.validation.gates import GateResult, ResultBundle
from trading_desk.validation.metrics import EvaluationArtifacts
from trading_desk.validation.oos import SealedOos

DIGEST = sha256_hex("agent-artifact")
POLICY_HASH = "a" * 64
WORKTREE = {
    "disposable": True,
    "path": "/tmp/worktrees/strategy-v1",
    "version_id": "ver-1",
}


def _bundle(kind: str) -> ResultBundle:
    return ResultBundle(
        kind=kind,
        outcome=GateResult.FAIL if kind == "development" else GateResult.PASS,
        gates={"max_drawdown": GateResult.PASS},
        metrics={"max_drawdown": "0.10"},
        policy_hash=POLICY_HASH,
        policy_version="validation-policy-v2",
    )


def _sealed() -> SealedOos:
    return SealedOos(
        EvaluationArtifacts(
            trades=(),
            starting_equity=Decimal("10000"),
            start=datetime(2020, 1, 1, tzinfo=UTC),
            end=datetime(2020, 7, 1, tzinfo=UTC),
        ),
        path="/sealed/oos.parquet",
        credentials="oos-token",
    )


def _ok_research(job):
    return {
        "artifacts": [],
        "job_id": job.job_id,
        "payload": {
            "mutation": {
                "expected_causal_effect": "fewer false breakouts",
                "files_and_fields": ["strategy/default.py:threshold"],
                "hypothesis": "raise threshold in high-vol regime",
                "invariant_diff": "unchanged",
            }
        },
        "pin": job.pin,
        "resolved_model_id": "gpt-5.6-sol",
        "status": "OK",
    }


def _ok_coding(job):
    return {
        "artifacts": [{"digest": DIGEST, "kind": "patch"}],
        "job_id": job.job_id,
        "payload": {
            "added_dependencies": [],
            "changed_files": ["src/trading_desk/strategy/default.py"],
            "commit": "b" * 40,
            "invariant_check": "unchanged",
            "test_results": {"failed": 0, "passed": True},
        },
        "pin": job.pin,
        "resolved_model_id": "deepseek-v4-flash",
        "status": "OK",
    }


def _ok_analysis(job):
    return {
        "artifacts": [],
        "job_id": job.job_id,
        "payload": {
            "analysis": {"loss_drivers": ["fees"]},
            "markdown_draft": "# Ledger\n",
            "mutation": None,
            "schema": "analysis-ledger-v1",
        },
        "pin": job.pin,
        "resolved_model_id": "kimi-k2.6",
        "status": "OK",
    }


def _ok_orchestrator(job):
    return {
        "artifacts": [],
        "job_id": job.job_id,
        "payload": {"result_handle": "research-job-1", "worker_profile": "research"},
        "pin": job.pin,
        "resolved_model_id": "deepseek-v4-pro",
        "status": "OK",
    }


def _research_job():
    return make_job(
        "research",
        "propose_mutation",
        research_inputs(development=_bundle("development"), ledger={"notes": "ok"}),
    )


def test_invalid_json_retries_same_pinned_job_then_agent_error_without_commit() -> None:
    commits: list[object] = []
    adapter = FakeProfileAdapter(["{", "{"], on_success=commits.append)
    job = _research_job()
    result = adapter.submit(job)
    assert result.status == AGENT_ERROR
    assert result.attempts == 2
    assert [item.pin for item in adapter.jobs] == [job.pin, job.pin]
    assert adapter.jobs[0].logical_model == adapter.jobs[1].logical_model == "gpt-5.6-sol"
    assert commits == []
    assert result.payload == {}


def test_schema_failure_retries_once_then_succeeds_on_same_pin() -> None:
    commits: list[object] = []
    adapter = FakeProfileAdapter(
        [{"status": "OK"}, _ok_research],
        on_success=commits.append,
    )
    job = _research_job()
    result = adapter.submit(job)
    assert result.status == "OK"
    assert result.attempts == 2
    assert [item.pin for item in adapter.jobs] == [job.pin, job.pin]
    assert len(commits) == 1
    assert commits[0] is result


def test_invalid_artifact_refs_retry_then_agent_error() -> None:
    bad = lambda job: {
        **_ok_research(job),
        "artifacts": [{"digest": "not-a-hash", "kind": "note"}],
    }
    adapter = FakeProfileAdapter([bad, bad])
    result = adapter.submit(_research_job())
    assert result.status == AGENT_ERROR
    assert result.attempts == 2


def test_provider_failure_is_agent_error_without_retry_or_fallback() -> None:
    adapter = FakeProfileAdapter([ProviderError("deepseek outage"), _ok_research])
    result = adapter.submit(_research_job())
    assert result.status == AGENT_ERROR
    assert result.attempts == 1
    assert len(adapter.jobs) == 1
    assert result.resolved_model_id is None


def test_profiles_pin_exact_logical_models_and_providers() -> None:
    expected = {
        "orchestrator": ("deepseek-v4-pro", "deepseek"),
        "research": ("gpt-5.6-sol", "openai-codex"),
        "coding": ("deepseek-v4-flash", "deepseek"),
        "analysis-ledger": ("kimi-k2.6", "kimi-coding"),
    }
    jobs = {
        "orchestrator": make_job(
            "orchestrator",
            "dispatch",
            orchestrator_inputs(action="dispatch", worker_profile="research"),
        ),
        "research": _research_job(),
        "coding": make_job(
            "coding",
            "implement",
            coding_inputs(development=_bundle("development"), mutation={"hypothesis": "x"}, worktree=WORKTREE),
        ),
        "analysis-ledger": make_job(
            "analysis-ledger",
            "draft_ledger",
            analysis_ledger_inputs(result=_bundle("development")),
        ),
    }
    for name, (model, provider) in expected.items():
        spec = PROFILE_CONFIGS[name]
        assert (spec.logical_model, spec.provider) == (model, provider)
        assert spec.fallback is False
        job = jobs[name]
        envelope = job.to_envelope()
        assert envelope["logical_model"] == model
        assert envelope["provider"] == provider
        assert "credential" not in canonical_json(envelope)


def test_job_rejects_model_override_and_response_fallback() -> None:
    with pytest.raises(ValueError, match="pinned|fallback"):
        make_job(
            "research",
            "propose_mutation",
            research_inputs(development=_bundle("development")),
            logical_model="gpt-4o",
        )
    adapter = FakeProfileAdapter(
        [lambda job: {**_ok_research(job), "resolved_model_id": "gpt-4o"}] * 2
    )
    result = adapter.submit(_research_job())
    assert result.status == AGENT_ERROR
    assert result.attempts == 2


def test_success_records_resolved_model_fingerprint() -> None:
    adapter = FakeProfileAdapter(
        [lambda job: {**_ok_research(job), "resolved_model_id": "gpt-5.6-sol-2026-03"}]
    )
    result = adapter.submit(_research_job())
    assert result.status == "OK"
    assert result.resolved_model_id == "gpt-5.6-sol-2026-03"
    assert result.resolved_fingerprint == sha256_hex(
        canonical_json(
            {
                "logical_model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "reasoning_effort": None,
                "resolved_model_id": "gpt-5.6-sol-2026-03",
                "thinking": False,
            }
        )
    )
    assert len(result.resolved_fingerprint) == 64


def test_credentials_never_enter_envelopes_or_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-deepseek-value")
    monkeypatch.setenv("KIMI_API_KEY", "sk-secret-kimi-value")
    job = make_job(
        "orchestrator",
        "dispatch",
        orchestrator_inputs(action="dispatch", worker_profile="coding"),
    )
    blob = canonical_json(job.to_envelope())
    assert "sk-secret-deepseek-value" not in blob
    assert "sk-secret-kimi-value" not in blob
    assert "DEEPSEEK_API_KEY" not in blob
    with pytest.raises(CapabilityError, match="credential"):
        research_inputs(development=_bundle("development"), api_key="sk-secret-deepseek-value")
    adapter = FakeProfileAdapter([_ok_orchestrator])
    result = adapter.submit(job)
    stored = ArtifactStore(tmp_path).put_json(result.to_payload())
    text = (tmp_path / stored[:2] / stored).read_text(encoding="utf-8")
    assert "sk-secret-deepseek-value" not in text
    assert "KIMI_API_KEY" not in text


def test_research_and_coding_reject_oos_paper_and_database_under_alternate_keys(
    tmp_path: Path,
) -> None:
    oos = _bundle("oos")
    db = Database(tmp_path / "trading_desk.sqlite3")
    with pytest.raises(CapabilityError, match="OOS"):
        research_inputs(holdout=oos)
    with pytest.raises(CapabilityError, match="OOS"):
        research_inputs(development=_bundle("development"), extra={"nested": oos.to_payload()})
    with pytest.raises(CapabilityError, match="OOS"):
        research_inputs(_sealed())
    with pytest.raises(CapabilityError, match="paper"):
        research_inputs(notes={"paper_account": {"credentials": "paper-key"}})
    with pytest.raises(CapabilityError, match="database"):
        research_inputs(handle=db)
    with pytest.raises(CapabilityError, match="OOS"):
        coding_inputs({"sealed": oos.to_payload()}, worktree=WORKTREE)
    with pytest.raises(CapabilityError, match="paper"):
        coding_inputs(misc={"paper_credentials": "x"}, worktree=WORKTREE)
    with pytest.raises(CapabilityError, match="database"):
        coding_inputs(sqlite_path=str(tmp_path / "state.sqlite3"), worktree=WORKTREE)
    payload = research_inputs(
        development=_bundle("development"),
        ledger={"notes": "ok"},
        invariants={"one_strategy": True},
        public_sources=[{"url": "https://example.com/research"}],
    )
    assert "oos" not in payload
    assert payload["development"]["kind"] == "development"


def test_coding_requires_disposable_worktree_metadata() -> None:
    with pytest.raises(CapabilityError, match="worktree"):
        coding_inputs(development=_bundle("development"), mutation={"hypothesis": "x"})
    with pytest.raises(CapabilityError, match="worktree"):
        coding_inputs(
            development=_bundle("development"),
            mutation={"hypothesis": "x"},
            worktree={"path": "/tmp/keep", "disposable": False, "version_id": "ver-1"},
        )
    bundle = coding_inputs(
        development=_bundle("development"),
        mutation={"hypothesis": "x"},
        worktree=WORKTREE,
    )
    job = make_job("coding", "implement", bundle)
    assert job.worktree == WORKTREE
    assert job.to_envelope()["worktree"]["disposable"] is True


def test_analysis_accepts_oos_result_bundle_only() -> None:
    oos = _bundle("oos")
    bundle = analysis_ledger_inputs(result=oos)
    assert bundle["result"]["kind"] == "oos"
    with pytest.raises(CapabilityError, match="result bundle only"):
        analysis_ledger_inputs(result=oos, ledger={"notes": "nope"})
    with pytest.raises(CapabilityError, match="credential"):
        analysis_ledger_inputs(result=oos, api_key="secret")
    job = make_job("analysis-ledger", "draft_ledger", bundle)
    adapter = FakeProfileAdapter(
        [lambda current: {**_ok_analysis(current), "payload": {**_ok_analysis(current)["payload"], "mutation": {"hypothesis": "from oos"}}}]
        * 2
    )
    result = adapter.submit(job)
    assert result.status == AGENT_ERROR


def test_analysis_rejects_independent_pass_fail_and_metric_changes() -> None:
    job = make_job(
        "analysis-ledger",
        "draft_ledger",
        analysis_ledger_inputs(result=_bundle("development")),
    )
    tainted = lambda current: {
        **_ok_analysis(current),
        "payload": {
            **_ok_analysis(current)["payload"],
            "analysis": {"outcome": "PASS", "metrics": {"max_drawdown": "0.01"}},
        },
    }
    result = FakeProfileAdapter([tainted, tainted]).submit(job)
    assert result.status == AGENT_ERROR


def test_orchestrator_forbids_validation_oos_approval_and_position_actions() -> None:
    for action in (
        "decide_validation",
        "modify_strategy",
        "read_oos",
        "approve_paper",
        "control_positions",
    ):
        with pytest.raises(CapabilityError, match="forbidden"):
            orchestrator_inputs(action=action, worker_profile="research")
    with pytest.raises(CapabilityError, match="OOS"):
        orchestrator_inputs(action="dispatch", worker_profile="research", holdout=_bundle("oos"))


def test_research_oauth_preflight_requires_exact_model_resolution() -> None:
    assert research_oauth_preflight(lambda model, provider: "gpt-5.6-sol-2026-03") == "gpt-5.6-sol-2026-03"
    with pytest.raises(ProfileUnavailable, match="research"):
        research_oauth_preflight(lambda model, provider: None)
    with pytest.raises(ProfileUnavailable, match="research"):
        research_oauth_preflight(lambda model, provider: "gpt-4o")


def test_coding_qualification_gate_requires_ten_tasks_at_90_percent() -> None:
    accepted = QualificationTaskResult(
        requested_change_only=True,
        invariants_unchanged=True,
        tests_passing=True,
        schema_valid=True,
        no_unapproved_dependency=True,
    )
    rejected = QualificationTaskResult(
        requested_change_only=False,
        invariants_unchanged=True,
        tests_passing=True,
        schema_valid=True,
        no_unapproved_dependency=True,
    )
    assert coding_qualification_gate((accepted,) * 10) is True
    assert coding_qualification_gate((accepted,) * 9 + (rejected,)) is True
    with pytest.raises(ProfileUnavailable, match="90"):
        coding_qualification_gate((accepted,) * 8 + (rejected,) * 2)
    with pytest.raises(ProfileUnavailable, match="10"):
        coding_qualification_gate((accepted,) * 9)


def test_hermes_adapter_is_typed_stub_without_network_imports() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "trading_desk" / "agents"
    forbidden = ("requests", "httpx", "openai", "anthropic", "http.client", "urllib.request", "subprocess")
    for name in ("hermes.py", "schemas.py", "capabilities.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            item == needle or item.startswith(needle + ".")
            for item in imported
            for needle in forbidden
        )
