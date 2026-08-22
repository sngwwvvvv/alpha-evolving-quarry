# AI Self-Improving Trading Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible Binance USDⓈ-M backtest and paper-trading desk whose deterministic Python core owns data, simulation, risk, validation, approvals, state, and evidence.

**Architecture:** Start as one Python modular monolith with SQLite WAL for authoritative state, immutable Parquet for market data/artifacts, and DuckDB for analytical reads. Implement one vertical slice from a fixed local dataset through backtest, validation, ledger bundle, and approval-gated paper simulation before adding live data and Hermes/Buzz adapters.

**Tech Stack:** Python 3.12+, standard library, SQLite WAL, Parquet, DuckDB, pytest, Git worktrees; Hermes/Buzz integration only behind typed adapters.

**Spec:** `docs/superpowers/specs/2026-08-23-ai-self-improving-trading-desk-design.md`

## Global Constraints

- MVP scope is backtest and paper trading only; no live trading, exchange testnet, paid data, or LLM-controlled order/risk decisions.
- Supported symbols are exactly `BTCUSDT`, `ETHUSDT`, `XRPUSDT`, and `SOLUSDT`; one strategy version has identical rules and numeric parameters across symbols.
- Invalid, missing, stale, unreconciled, or ambiguous inputs fail closed.
- Development may inform one successor mutation; sealed OOS is terminal evidence and cannot enter research/coding inputs.
- Maximum performance-evaluated versions per family is 8; maximum sealed OOS evaluations per family is 1; technical errors consume neither budget.
- `MDD < 15%` is a hard gate; paper starts and resumes only after explicit approval bound to exact hashes.
- SQLite state transition and budget changes are transactional; artifacts are hashed and atomically renamed before reference commit.
- No core queue, Redis, Kafka, Postgres, microservice split, or speculative abstraction is added.

## File Map

- Create `pyproject.toml`, `src/trading_desk/`, and `tests/` for the runnable core.
- Create `src/trading_desk/data/` for ingestion, validation, aggregation, snapshots, and macro provenance.
- Create `src/trading_desk/strategy/` for the immutable strategy contract and default strategy family.
- Create `src/trading_desk/backtest/` for causality-safe execution and paper-equivalent lifecycle code.
- Create `src/trading_desk/validation/` for metrics, gates, walk-forward, bootstrap, PSR/DSR, neighborhood, and leave-one-out checks.
- Create `src/trading_desk/state/` for SQLite schema, transitions, approvals, budgets, ledger, and outbox.
- Create `src/trading_desk/agents/` for strict job envelopes and isolated Hermes-profile adapters.
- Create `src/trading_desk/paper/` for real-time feed, fill simulation, stale-data handling, and reconciliation.
- Create `src/trading_desk/publish/` for versioned Markdown/JSON bundles and retryable publication.
- Create `docs/operations/` for deployment, secrets, profile boundaries, and recovery runbooks.

### Task 1: Bootstrap the deterministic core and repository checks

**Files:**
- Create: `pyproject.toml`
- Create: `src/trading_desk/__init__.py`
- Create: `src/trading_desk/config.py`
- Create: `tests/test_bootstrap.py`

**Interfaces:** `Settings`, fixed symbol tuple, UTC/time/hash helpers, and a test command usable by every later task.

- [ ] Step 1: Add the minimum package metadata and test configuration; do not add an unneeded framework.
- [ ] Step 2: Define immutable settings for the four symbols, UTC timezone, artifact root, SQLite path, and policy version.
- [ ] Step 3: Add a self-check that settings reject unsupported symbols and produce stable canonical JSON/hash output.
- [ ] Step 4: Run `python -m pytest tests/test_bootstrap.py -q` and confirm PASS.

### Task 2: Implement immutable storage, identifiers, and SQLite state

**Files:**
- Create: `src/trading_desk/storage/artifacts.py`
- Create: `src/trading_desk/state/db.py`
- Create: `src/trading_desk/state/schema.sql`
- Create: `tests/test_storage_state.py`

**Interfaces:** `ArtifactStore.put_bytes()`, `ArtifactStore.put_json()`, `RunIdentity`, and transactional repositories for families, versions, runs, transitions, approvals, budgets, paper state, and outbox rows.

- [ ] Step 1: Test that canonical artifact bytes are hashed, written to a temporary path, atomically renamed, and never overwritten.
- [ ] Step 2: Test that a strategy version is immutable and a rerun receives a new `run_id`.
- [ ] Step 3: Implement SQLite WAL initialization and the schema with foreign keys, unique idempotency keys, append-only transitions, and the required hash identifiers.
- [ ] Step 4: Test that state transition plus budget consumption commits together and rolls back together.
- [ ] Step 5: Run `python -m pytest tests/test_storage_state.py -q` and confirm PASS.

### Task 3: Build market-data contracts and deterministic derivation

**Files:**
- Create: `src/trading_desk/data/contracts.py`
- Create: `src/trading_desk/data/validate.py`
- Create: `src/trading_desk/data/aggregate.py`
- Create: `src/trading_desk/data/snapshot.py`
- Create: `tests/data/test_market_data.py`

**Interfaces:** typed 1-minute kline, funding, contract metadata, macro event, `DataSnapshot`, `validate_snapshot()`, `derive_hourly_bars()`, and `derive_utc_daily_bars()`.

- [ ] Step 1: Write fixtures containing duplicate, reversed, invalid-OHLC, missing-bar, metadata-gap, and valid four-symbol cases.
- [ ] Step 2: Test that invalid data returns `DATA_BLOCKED`, performs no interpolation/forward-fill, and records source/transformation hashes.
- [ ] Step 3: Implement immutable partition validation and deterministic 1-hour/UTC-daily aggregation using completed bars only.
- [ ] Step 4: Test common evaluation start as the latest valid four-symbol timestamp plus EMA-200 daily warm-up.
- [ ] Step 5: Run `python -m pytest tests/data -q` and confirm PASS.

### Task 4: Implement the immutable strategy contract and causal execution

**Files:**
- Create: `src/trading_desk/strategy/models.py`
- Create: `src/trading_desk/strategy/default.py`
- Create: `src/trading_desk/backtest/execution.py`
- Create: `src/trading_desk/backtest/account.py`
- Create: `tests/strategy/test_contract.py`
- Create: `tests/backtest/test_causality.py`

**Interfaces:** `StrategyVersion`, `StrategyFamily`, `StrategySignal`, `Position`, `ExecutionPolicy`, `BacktestEngine.run()`, and `PaperLifecycle` shared by backtest and paper modes.

- [ ] Step 1: Test identical signal graph, parameters, TP/SL, lifecycle, leverage, and risk formula across all four symbols; reject symbol lookup tables and asset branches.
- [ ] Step 2: Test completed-hour signal timing, next-hour entry, completed-daily EMA-50/EMA-200 regime timing, funding publication timing, quantity rounding, minimum notional, fees, slippage, and funding direction.
- [ ] Step 3: Implement conservative same-minute TP/SL ambiguity and deterministic account/equity/position accounting.
- [ ] Step 4: Test 0.5% per-position planned risk, 2.0% aggregate planned risk, 2x isolated leverage, gross-leverage ceiling, daily loss stop, and exact 15% MDD halt/rejection.
- [ ] Step 5: Run `python -m pytest tests/strategy tests/backtest -q` and confirm PASS.

### Task 5: Add validation policy v2 and sealed OOS control

**Files:**
- Create: `src/trading_desk/validation/policy_v2.yaml`
- Create: `src/trading_desk/validation/metrics.py`
- Create: `src/trading_desk/validation/gates.py`
- Create: `src/trading_desk/validation/walk_forward.py`
- Create: `src/trading_desk/validation/statistics.py`
- Create: `src/trading_desk/validation/oos.py`
- Create: `tests/validation/test_policy_v2.py`

**Interfaces:** `EvaluationPolicy`, `ResultBundle`, `evaluate_development()`, `evaluate_oos_once()`, and `GateResult(PASS|FAIL)`.

- [ ] Step 1: Copy the exact v2 policy values and semantics from the spec, including seed `20260823`, block bootstrap, PSR/DSR, concentration, neighborhood, and leave-one-out rules.
- [ ] Step 2: Test every hard boundary: `MDD == 15%` fails, positive return is strict, PF lower bound is strict where specified, and all gates are independent.
- [ ] Step 3: Implement chronological non-overlapping walk-forward evaluation and deterministic metrics over persisted trade/result artifacts.
- [ ] Step 4: Implement family budgets, one mutation per successor, one sealed OOS run, rejection, and prohibition on OOS-to-research inputs.
- [ ] Step 5: Add a fixed golden dataset test asserting stable result-bundle hashes.
- [ ] Step 6: Run `python -m pytest tests/validation -q` and confirm PASS.

### Task 6: Implement approvals, state machine, and deterministic outbox

**Files:**
- Create: `src/trading_desk/state/transitions.py`
- Create: `src/trading_desk/state/approvals.py`
- Create: `src/trading_desk/state/outbox.py`
- Create: `tests/state/test_transitions_approvals.py`

**Interfaces:** `transition()`, `validate_approval()`, `ApprovalCommand`, and `claim_outbox_due()`.

- [ ] Step 1: Test every spec transition, including technical rerun paths, development mutation path, OOS terminal path, paper daily pause, MDD halt, and data-gap halt.
- [ ] Step 2: Test allowlisted identity, exact action/object hashes, source-command hash, timestamp, idempotency, stale/superseded rejection, and ambiguous payload rejection.
- [ ] Step 3: Implement transactional precondition validation so agents can request but never commit transitions.
- [ ] Step 4: Test deterministic alert publication through the outbox without LLM dependency.
- [ ] Step 5: Run `python -m pytest tests/state -q` and confirm PASS.

### Task 7: Connect the end-to-end development/OOS/ledger bundle

**Files:**
- Create: `src/trading_desk/workflows/research_loop.py`
- Create: `src/trading_desk/ledger/bundle.py`
- Create: `tests/test_end_to_end.py`

**Interfaces:** `run_development_cycle()`, `run_sealed_oos()`, `ResultBundle`, `LedgerBundle`, and immutable mutation manifest.

- [ ] Step 1: Test a fixed dataset flowing through snapshot validation, backtest, all gates, persistence, and evidence hashing.
- [ ] Step 2: Implement the loop: register version, evaluate development, record analysis request on failure, allow exactly one successor mutation, then automatically run the first sealed OOS after development pass.
- [ ] Step 3: Implement ledger content with gate results, loss attribution, prior-version comparison, run/trade references, and no mutation proposal for OOS failure.
- [ ] Step 4: Test that OOS artifacts are analyzable but cannot be present in research/coding input bundles.
- [ ] Step 5: Run `python -m pytest tests/test_end_to_end.py -q` and confirm PASS.

### Task 8: Add agent envelopes and isolated profile adapters

**Files:**
- Create: `src/trading_desk/agents/schemas.py`
- Create: `src/trading_desk/agents/capabilities.py`
- Create: `src/trading_desk/agents/hermes.py`
- Create: `tests/agents/test_boundaries.py`
- Create: `docs/operations/hermes-profiles.md`

**Interfaces:** `AgentJob`, `AgentResult`, `ProfileAdapter.submit()`, and capability-checked input bundles for `orchestrator`, `research`, `coding`, and `analysis-ledger`.

- [ ] Step 1: Test strict JSON schema validation, one retry with the same pinned job, then `AGENT_ERROR` with no state mutation.
- [ ] Step 2: Implement profile configuration for the exact logical models/providers, no fallback, resolved model fingerprint recording, and credential exclusion from envelopes/artifacts.
- [ ] Step 3: Enforce research/coding/analysis capability boundaries, coding disposable worktree metadata, and OOS/paper/database exclusions.
- [ ] Step 4: Add a preflight contract for Research OAuth model resolution and the Coding Agent 10-task/90%-acceptance qualification gate.
- [ ] Step 5: Run `python -m pytest tests/agents -q` and confirm PASS.

### Task 9: Implement paper feed, fills, stale handling, and reconciliation

**Files:**
- Create: `src/trading_desk/paper/feeds.py`
- Create: `src/trading_desk/paper/fills.py`
- Create: `src/trading_desk/paper/reconcile.py`
- Create: `tests/paper/test_recovery.py`

**Interfaces:** `PaperFeed`, `FillAdapter`, `PaperEngine`, `reconcile_chronologically()`, and explicit approval resume hooks.

- [ ] Step 1: Test fresh required streams, legal rounded entries, conservative observable fills, optional analysis-only streams, and no signal/risk change from those streams.
- [ ] Step 2: Test 60-second staleness -> cancel pending entries/open no new risk while reductions remain allowed.
- [ ] Step 3: Implement REST chronological gap repair; unreconciled gaps enter `PAPER_DATA_GAP` and require approval after repair.
- [ ] Step 4: Test daily-loss automatic next-UTC-day resume versus manual MDD/data-gap resume.
- [ ] Step 5: Run `python -m pytest tests/paper -q` and confirm PASS.

### Task 10: Add publish bundles, retry schedule, and operational verification

**Files:**
- Create: `src/trading_desk/publish/publisher.py`
- Create: `tests/publish/test_retries.py`
- Create: `docs/operations/recovery.md`
- Create: `docs/operations/security.md`
- Create: `tests/test_minimum_verification.py`

**Interfaces:** `PublicationRevision`, `publish_revision()`, and retry scheduling at immediate/5m/15m/60m/6h with 24h warning.

- [ ] Step 1: Test idempotent revision publication, append-only ledger revisions, wiki isolation from SQLite, and non-blocking publication failure.
- [ ] Step 2: Implement Markdown/JSON bundle generation and outbox retry state (`attempt_count`, `next_attempt_at`, `published_revision_id`).
- [ ] Step 3: Add the minimum verification matrix from spec section 18, prioritizing golden hashes, boundaries, sealing, approvals, recovery, agent violations, and end-to-end flow.
- [ ] Step 4: Document secrets per profile, no paper live credentials, allowlisted egress, policy-hash changes, and recovery commands.
- [ ] Step 5: Run `python -m pytest -q` and confirm the full suite passes.

## Delivery Gates

1. Gate A: Tasks 1–4 pass; deterministic backtest on a fixed local dataset.
2. Gate B: Tasks 5–7 pass; development gates, sealed OOS, evidence bundle, and mutation budgets are enforced.
3. Gate C: Tasks 8–9 pass; agents are advisory and paper is approval/reconciliation gated.
4. Gate D: Task 10 passes; full verification suite and operational/security review are complete.

Implementation must stop for explicit user review if a spec boundary needs weakening. No live exchange or testnet execution is added to this MVP.

