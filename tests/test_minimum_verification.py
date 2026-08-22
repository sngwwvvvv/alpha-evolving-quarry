"""Minimum verification matrix for spec section 18.

Reuses representative existing tests rather than copying their bodies.
New publication coverage lives in tests/publish/test_retries.py and the
end-to-end publish assertion below.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import pytest

from trading_desk.publish.publisher import FakeWikiSink, publish_revision
from trading_desk.workflows.research_loop import run_development_cycle

TESTS = Path(__file__).resolve().parent
_MODULES: dict[str, Any] = {}

SPEC_18: tuple[tuple[str, str, str], ...] = (
    ("1. golden hashes", "validation/test_policy_v2.py", "test_golden_result_bundle_hash_is_stable"),
    ("2. cross-asset equality", "strategy/test_contract.py", "test_default_family_uses_one_topology_for_every_symbol"),
    ("3. planned-risk caps", "backtest/test_causality.py", "test_size_order_caps_per_position_and_aggregate_planned_risk"),
    ("4. leverage ceiling", "backtest/test_causality.py", "test_gross_leverage_ceiling_and_isolated_two_x"),
    ("5. no-lookahead", "backtest/test_causality.py", "test_completed_hour_signal_fills_on_next_hour_open"),
    ("6. fees rounding min order", "backtest/test_causality.py", "test_quantity_is_rounded_to_step_and_tiny_notional_is_rejected"),
    ("7. same-minute TP/SL", "backtest/test_causality.py", "test_same_minute_tp_and_sl_uses_stop_first"),
    ("8. gate boundaries", "validation/test_policy_v2.py", "test_hard_boundaries_and_independent_gates"),
    ("9. exact 15% MDD", "backtest/test_causality.py", "test_exact_fifteen_percent_mdd_fails"),
    ("10. OOS sealing", "validation/test_policy_v2.py", "test_sealed_oos_once_rejection_and_research_prohibition"),
    ("11. READY_FOR_PAPER approval", "state/test_transitions_approvals.py", "test_ready_for_paper_starts_only_with_exact_approval"),
    ("12. daily vs MDD resume", "paper/test_recovery.py", "test_daily_loss_resumes_automatically_next_utc_day"),
    ("13. approval rejection", "state/test_transitions_approvals.py", "test_approval_rejects_stale_superseded_ambiguous_and_conflicting_idempotency"),
    ("14. recovery faults", "paper/test_recovery.py", "test_reconcile_rejects_reversed_and_duplicate_input_without_sorting"),
    ("15. agent violations", "agents/test_boundaries.py", "test_research_and_coding_reject_oos_paper_and_database_under_alternate_keys"),
    ("16. e2e publish bundle", "test_end_to_end.py", "test_fixed_dataset_flows_through_snapshot_backtest_gates_persistence_and_hashes"),
    ("17. wiki retry idempotent", "publish/test_retries.py", "test_retry_schedule_immediate_5m_15m_60m_6h_and_24h_warning"),
)


def _load(rel: str) -> Any:
    if rel not in _MODULES:
        path = TESTS / rel
        spec = importlib.util.spec_from_file_location(f"matrix_{rel.replace('/', '_')}", path)
        if spec is None or spec.loader is None:
            raise ImportError(rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[rel] = module
    return _MODULES[rel]


def _call(rel: str, name: str, *args: Any) -> None:
    fn: Callable[..., None] = getattr(_load(rel), name)
    fn(*args)


def test_spec_18_matrix_points_at_existing_tests() -> None:
    assert len(SPEC_18) == 17
    for label, rel, name in SPEC_18:
        assert hasattr(_load(rel), name), f"{label}: {rel}::{name}"


def test_s18_golden_hashes() -> None:
    _call("validation/test_policy_v2.py", "test_golden_result_bundle_hash_is_stable")


def test_s18_boundaries_and_cross_asset() -> None:
    _call("validation/test_policy_v2.py", "test_hard_boundaries_and_independent_gates")
    _call("strategy/test_contract.py", "test_default_family_uses_one_topology_for_every_symbol")
    _call("strategy/test_contract.py", "test_parameters_reject_symbol_lookup_tables")
    _call("backtest/test_causality.py", "test_size_order_caps_per_position_and_aggregate_planned_risk")
    _call("backtest/test_causality.py", "test_gross_leverage_ceiling_and_isolated_two_x")


def test_s18_oos_sealing(tmp_path: Path) -> None:
    _call("validation/test_policy_v2.py", "test_sealed_oos_once_rejection_and_research_prohibition", tmp_path)


def test_s18_approvals(tmp_path: Path) -> None:
    _call(
        "state/test_transitions_approvals.py",
        "test_ready_for_paper_starts_only_with_exact_approval",
        tmp_path / "ready",
    )
    _call(
        "state/test_transitions_approvals.py",
        "test_approval_rejects_stale_superseded_ambiguous_and_conflicting_idempotency",
        tmp_path / "reject",
    )


def test_s18_recovery(tmp_path: Path) -> None:
    _call("paper/test_recovery.py", "test_rest_gap_repair_replays_chronologically", tmp_path / "rest")
    _call("paper/test_recovery.py", "test_daily_loss_resumes_automatically_next_utc_day", tmp_path / "daily")
    _call("paper/test_recovery.py", "test_mdd_halt_requires_explicit_approval", tmp_path / "mdd")
    _call("paper/test_recovery.py", "test_reconcile_rejects_reversed_and_duplicate_input_without_sorting")


def test_s18_agent_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _call(
        "agents/test_boundaries.py",
        "test_credentials_never_enter_envelopes_or_artifacts",
        tmp_path / "creds",
        monkeypatch,
    )
    _call(
        "agents/test_boundaries.py",
        "test_research_and_coding_reject_oos_paper_and_database_under_alternate_keys",
        tmp_path / "caps",
    )
    _call("agents/test_boundaries.py", "test_invalid_json_retries_same_pinned_job_then_agent_error_without_commit")


def test_s18_end_to_end_data_backtest_validation_ledger_publish(tmp_path: Path) -> None:
    e2e = _load("test_end_to_end.py")
    db, store, snapshot = e2e._harness(tmp_path)
    cycle = run_development_cycle(
        db,
        store,
        snapshot,
        code_commit=e2e.COMMIT,
        policy=e2e.POLICY,
        starting_equity=e2e.STARTING_EQUITY,
    )
    assert cycle.backtest is not None
    assert cycle.result is not None
    assert cycle.ledger is not None
    assert cycle.artifact_digests["ledger"]
    sink = FakeWikiSink()
    revision = publish_revision(db, store, cycle.ledger, sink=sink, namespace="backtest")
    assert revision.namespace == "backtest"
    assert cycle.ledger.executive_summary in revision.markdown
    assert cycle.ledger.bundle_hash in revision.json_payload.get("bundle_hash", revision.bundle_hash)
    assert sink.get(revision.revision_id) is not None
    page = sink.get(revision.revision_id)
    assert page is not None
    assert page.markdown == revision.markdown
