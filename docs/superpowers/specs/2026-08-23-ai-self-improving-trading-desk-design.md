# AI Self-Improving Trading Desk — Architecture Specification

- Status: review candidate
- Date: 2026-08-23
- Scope: backtest and paper trading only
- Authority: this document supersedes `initial_idea.md` where they differ

## 1. Purpose

Build a reproducible AI-assisted research desk for Binance USDⓈ-M perpetual futures. The system may propose and implement strategy mutations, but deterministic Python code owns market data, simulation, risk, validation, approvals, state transitions, and persistence.

The MVP must produce a strategy that survives development validation and one sealed out-of-sample evaluation before it can become eligible for paper trading. Passing does not authorize paper execution: only an explicit user approval can start it.

This is not an autonomous money manager. Live trading, exchange testnet trading, paid data, and LLM-controlled order or risk decisions are outside the MVP.

## 2. Design principles

1. **Deterministic core, probabilistic advisors.** LLM agents propose work and prepare analyses; they cannot decide a gate, change risk controls, approve transitions, or write authoritative state.
2. **One strategy everywhere.** A strategy version uses the same rules, numeric parameters, TP/SL logic, and risk formula on BTC, ETH, XRP, and SOL.
3. **Development can learn; OOS cannot.** Development results may drive one mutation. Sealed OOS results are terminal evidence and must not be fed back into optimization.
4. **Fail closed.** Invalid or stale data, ambiguous approval, invalid agent output, and unreconciled paper state prevent new risk.
5. **Immutable evidence.** Raw inputs, strategy versions, evaluation policies, and result bundles are hashed and append-only.
6. **Small operational footprint.** The trading core is a Python modular monolith using files and SQLite. No core queue service, Redis, Kafka, Postgres, or microservice split is introduced in the MVP.

## 3. Product boundary

### 3.1 Included

- Binance USDⓈ-M perpetual symbols:
  - `BTCUSDT`
  - `ETHUSDT`
  - `XRPUSDT`
  - `SOLUSDT`
- Historical acquisition and validation of Binance 1-minute klines, funding, and contract metadata.
- Deterministic derivation of 1-hour and UTC daily bars.
- Development walk-forward backtests.
- One sealed six-month OOS evaluation per strategy family.
- Agent-assisted research, coding, and loss analysis.
- Paper execution after explicit user approval.
- A separate read-only wiki and Buzz-based discussion, notifications, and approval intake.

### 3.2 Excluded

- Live trading or custody of real funds.
- Exchange testnet order execution in the MVP.
- Paid market or macro data.
- Backtest use of order books, individual trades, liquidations, long/short ratios, or open interest.
- News, sentiment, macro data, or microstructure data as entry/exit signals or regime inputs.
- Asset-specific parameters or rules.
- Automatic strategy-family reset after OOS rejection.
- Automatic model fallback.

## 4. System boundary and architecture

```text
                            authenticated approval / discussion
 User <---------------------------- Buzz -----------------------------+
   |                                                                  |
   | read                                                             | native gateway
   v                                                                  v
Static Wiki <--- versioned Markdown/JSON bundle --- Publisher   Hermes Orchestrator
                                                               /        |         \
                                                      Research Profile  Coding Profile  Analysis Profile
                                                               \        |         /
                                                                strict JSON jobs
                                                                      |
                                                                      v
+----------------------------------------------------------------------------------+
|                            Deterministic Python Core                             |
| Market Data | Strategy Registry | Experiment Engine | Validation & Ledger       |
| Paper Engine | Approval/State Machine | Agent Adapter | Outbox/Publisher          |
+----------------------+----------------------+----------------------+---------------+
                       |                      |                      |
                    Parquet                DuckDB              SQLite WAL
                immutable series       analytical queries   state/approvals/runs
```

The core is one deployable codebase with internal module boundaries. A module may run as a separate process when needed, but it remains part of the same release and schema contract.

The wiki is separated from day one because its rendered history can grow independently. It receives publish bundles and never opens the trading core's SQLite database. Buzz is also an external collaboration subsystem; any Postgres, Redis, or object storage used by Buzz is not part of the trading core architecture.

## 5. Authoritative storage and identities

### 5.1 Storage

- **Parquet:** immutable raw and derived time-series partitions.
- **DuckDB:** read-oriented analytical queries over Parquet and result artifacts.
- **SQLite in WAL mode:** runs, strategy families and versions, state transitions, approvals, budgets, ledger metadata, paper account state, and outbox rows.
- **Versioned artifacts:** result bundles, trade lists, plots, agent job envelopes, JSON responses, and rendered Markdown.

Structured data in SQLite and versioned result artifacts is the source of truth. Markdown wiki pages are projections for humans and may be regenerated.

### 5.2 Required identifiers

Every run and publication must be traceable by:

- `family_id`
- `strategy_version_id`
- `code_commit`
- `data_snapshot_hash`
- `derived_data_hash`
- `validation_policy_hash`
- `execution_policy_hash`
- `run_id`
- `agent_job_id`, when an agent participated
- `model_provider`, `model_id`, model configuration, and provider fingerprint when available

A strategy version is immutable after registration. A rerun creates a new `run_id`, not a modified result.

## 6. Market data contract

### 6.1 Backtest data

Backtests may use only:

- Binance 1-minute OHLCV klines.
- Funding rates and funding timestamps.
- Contract metadata needed for price tick, quantity step, minimum quantity/notional, and listing state.

The core derives 1-hour and UTC daily bars from completed 1-minute bars. Strategy signals are evaluated only on completed 1-hour bars. Daily regime is evaluated only from completed UTC daily bars.

Raw partitions are immutable. Corrections are new snapshots, never in-place edits. Derived partitions record their source hashes and transformation version.

### 6.2 Common evaluation span and warm-up

All four symbols must be evaluated together. The first evaluable timestamp is the latest timestamp at which every symbol has valid common data plus enough completed daily history for EMA-200 warm-up. Earlier symbol-specific history may be stored but cannot be used for four-asset selection metrics.

### 6.3 Data-quality rules

The following conditions fail closed:

- Missing required bars or funding records.
- Duplicate primary keys.
- Reversed or non-monotonic timestamps.
- Invalid OHLC relationships or negative volume.
- Contract metadata gaps that make a legal order size unknowable.
- Derived data whose source hash cannot be reproduced.

OHLCV must never be interpolated or forward-filled. A technical data failure becomes `DATA_BLOCKED` and consumes no strategy or OOS budget.

### 6.4 Paper-only real-time data

Paper trading may collect Binance real-time:

- order book updates;
- individual trades;
- liquidation events;
- long/short ratio;
- open interest.

These fields may improve fill simulation or become analysis tags. They may not change the strategy's entry/exit rules, regime, TP/SL, position size, or risk controls. Long/short ratio, open interest, and liquidations are analysis-only unless a future specification changes both backtest and paper contracts together.

If required real-time price or account state is stale for 60 seconds, the paper engine enters `DATA_STALE`, cancels pending entry intents, and opens no new position. It may still reduce risk. On reconnect it fills gaps chronologically through Binance REST. Any unreconciled gap becomes `PAPER_DATA_GAP`, halts the engine, notifies the user, and requires explicit user approval after repair; it does not auto-resume.

## 7. Macro event contract

Macro events are collected from free sources only. The system stores released actual values, event time, publication time, unit, source, and revision/vintage when available. Consensus or expected values are neither required nor derived.

Macro events are attached to trades and performance periods solely for attribution. They are forbidden inputs to:

- entry or exit signals;
- the daily regime;
- position sizing;
- TP/SL;
- validation pass/fail.

No particular provider is mandated. A source adapter is acceptable only if it has free access, provides actual released values, preserves publication timestamps, and passes provenance and duplication checks.

## 8. Strategy contract

### 8.1 Cross-asset invariants

For a given `strategy_version_id`, all four symbols use exactly the same:

- signal graph and indicators;
- lookback values and thresholds;
- long and short rules;
- TP/SL method and parameters;
- position lifecycle rules;
- leverage and risk formula;
- execution and cost assumptions.

The symbol may be an input only for its own price/contract data and legal order rounding. No symbol lookup table, symbol coefficient, or asset-specific branch is allowed.

### 8.2 Daily regime

The deterministic regime uses EMA-50 and EMA-200 computed from completed UTC daily close bars:

- `BULL`: EMA-50 > EMA-200 — long entries may be considered; short entries are blocked.
- `BEAR`: EMA-50 < EMA-200 — short entries may be considered; long entries are blocked.
- equality or unavailable warm-up: no new entry.

Regime is an entry filter only. A regime change does not close, resize, or otherwise manage an existing position.

### 8.3 Execution causality

- A signal can use only information whose publication timestamp is at or before the completed signal bar.
- An order can fill no earlier than the next available 1-minute execution step after signal completion.
- Fees, slippage, funding, quantity rounding, and rejected-too-small orders are part of every result.
- When both TP and SL are reachable inside the same 1-minute bar and ordering cannot be proven, the stop is assumed to execute first.
- Baseline fee and slippage values are versioned execution-policy inputs frozen before a family starts; they cannot be tuned per strategy version.

## 9. Risk contract

The following are system invariants and cannot be mutated by agents:

- Margin mode: isolated.
- Leverage: 2x for every symbol.
- Maximum gross effective leverage: 2x across the account.
- Initial planned loss per position: 0.5% of current equity, including modeled fees and slippage.
- Maximum sum of open planned risk: 2.0% of current equity.
- The initial 0.5% risk reservation remains reserved until the position closes, even if a stop later moves toward break-even or profit.
- Sizing order: determine stop distance and allowed planned loss first, derive unlevered notional, then apply the 2x leverage and exchange rounding constraints. Leverage never determines the risk budget.

### 9.1 Daily loss stop

At a 2.0% loss relative to equity at 00:00 UTC:

1. cancel pending orders;
2. close open positions using the conservative execution model;
3. prohibit new entries until the next UTC day;
4. resume automatically at the next UTC boundary if no stronger halt is active.

### 9.2 Drawdown stop

Drawdown is measured from the account equity high-water mark. At 15.0% MDD:

- backtest: the strategy fails because the hard gate is strictly `MDD < 15%`;
- paper: cancel orders, close positions, enter `PAPER_MDD_HALTED`, notify the user, and require explicit user approval to resume.

The non-blocking target is MDD ≤10%. Exactly 15.0% is a failure.

## 10. Self-improvement loop

### 10.1 Strategy family

A family groups versions that share the same signal topology, feature set, entry/exit mechanism, and position lifecycle design. Numeric parameter, threshold, or TP/SL value changes remain in the same family.

A new family requires a material structural change to at least one of those four elements. Renaming, refactoring, changing only constants, changing an LLM prompt, or changing validation/execution assumptions does not qualify. Core risk and cross-asset invariants may never be changed to manufacture a new family.

### 10.2 One mutation per version

Each successor version must contain exactly one declared logical mutation. A mutation may touch multiple lines only when they implement the same hypothesis. The mutation manifest records:

- prior version;
- hypothesis;
- files and configuration fields changed;
- expected causal effect;
- invariant diff result.

Unrelated cleanup is prohibited in a strategy mutation. If necessary, maintenance runs separately and cannot alter strategy semantics.

### 10.3 Loop sequence

1. Register immutable strategy version.
2. Run deterministic development evaluation.
3. Validate every independent gate.
4. If development fails, the Analysis/Ledger Agent records causes.
5. The Research Agent may propose exactly one new development mutation.
6. The Coding Agent implements and verifies only that mutation.
7. Repeat until pass, budget exhaustion, or unrecoverable error.
8. On development pass, the core automatically performs the family's first and only sealed OOS evaluation.
9. OOS pass produces `READY_FOR_PAPER`; it never starts paper automatically.
10. Paper begins only after explicit approval tied to the exact strategy, data, execution, and validation hashes.

### 10.4 Budgets and OOS contamination control

- Maximum performance-evaluated versions per family: 8.
- Every performance-evaluated version counts in DSR trial accounting, including failed versions.
- Maximum sealed OOS evaluations per family: 1.
- Technical `RUN_ERROR`, `DATA_BLOCKED`, or agent-format errors consume no strategy or OOS budget.
- OOS artifacts may be analyzed and recorded but must not be included in Research or Coding Agent mutation inputs.
- After OOS failure, the family is `REJECTED`.
- Another OOS evaluation is allowed only for a materially changed new family and only after explicit user approval naming the rejected family and proposed new family.

## 11. Validation policy v2

There is one result: `PASS` or `FAIL`. Every hard gate is independent and mandatory. Targets are reported as quality goals and never create a conditional pass.

```yaml
version: validation-policy-v2

development:
  profitability:
    cagr:
      target_minimum: 0.12
      gate_minimum: 0.10
    calmar:
      target_minimum: 1.00
      gate_minimum: 0.75

  risk:
    max_drawdown:
      target_maximum: 0.10
      gate_exclusive_maximum: 0.15
      scope: [aggregate, each_walk_forward_window]

  walk_forward:
    window_months: 6
    overlap: false
    positive_window_fraction:
      target_minimum: 0.70
      gate_minimum: 0.60

  trades:
    aggregate:
      target_minimum: 200
      gate_minimum: 120
    per_symbol:
      target_minimum: 30
      gate_minimum: 20

  profit_factor:
    base_point_estimate:
      target_minimum: 1.30
      gate_minimum: 1.20
    stress_point_estimate:
      target_minimum: 1.15
      gate_minimum: 1.05
    bootstrap:
      confidence: 0.90
      target_lower_bound_minimum: 1.05
      gate_lower_bound_exclusive_minimum: 1.00
      block_days: 30
      resamples: 5000
      seed: 20260823
      algorithm: trade-entry-moving-block-v1
      preserve: [whole_trades, cross_symbol_entry_clusters]

  execution_stress:
    fee_multiplier: 1.5
    slippage_multiplier: 2.0
    adverse_paid_funding_multiplier: 1.5
    received_funding_multiplier: 0.5

  statistical_confidence:
    psr:
      return_frequency: weekly_utc
      benchmark_sharpe: 0.50
      target_probability_minimum: 0.95
      gate_probability_minimum: 0.90
    dsr:
      target_probability_minimum: 0.99
      gate_probability_minimum: 0.95
      trial_count: every_performance_evaluated_version
      correlation_treatment: daily-psr-dsr-v1

  concentration:
    symbol:
      target_share_maximum: 0.50
      gate_share_maximum: 0.70
    direction:
      target_share_maximum: 0.70
      gate: report_only
    period:
      bucket: walk_forward_window
      target_share_maximum: 0.50
      gate_share_maximum: 0.70

  neighborhood:
    perturbation: one_parameter_at_a_time_plus_minus_10_percent
    survival:
      total_return_minimum: 0.00
      profit_factor_minimum: 1.00
      max_drawdown_maximum: 0.20
    target_survival_fraction: 0.70
    gate_survival_fraction_minimum: 0.50

  leave_one_symbol_out:
    cases: 4
    survival:
      total_return_minimum: 0.00
      profit_factor_minimum: 1.00
      max_drawdown_maximum: 0.20
    target_surviving_count: 4
    gate_surviving_count_minimum: 3

oos:
  sealed_window_months: 6
  evaluations_per_family: 1
  trades:
    target_minimum: 40
    gate_minimum: 20
  total_return:
    target_minimum: 0.05
    gate_exclusive_minimum: 0.00
  profit_factor:
    target_minimum: 1.20
    gate_minimum: 1.05
  max_drawdown:
    target_maximum: 0.10
    gate_exclusive_maximum: 0.15
  psr: report_only
  dsr: not_recomputed
  bootstrap: not_run
  neighborhood: not_run
  leave_one_symbol_out: not_run

budget:
  max_performance_evaluated_versions_per_family: 8
  mutations_per_successor_version: 1
  max_oos_evaluations_per_family: 1
  technical_errors_consume_budget: false
```

### 11.1 Metric semantics

- Six-month walk-forward windows are chronological, non-overlapping, and use the same frozen execution policy.
- A positive window has total return strictly greater than zero.
- Profit factor is gross profit divided by absolute gross loss after all modeled costs.
- Concentration share is a bucket's positive net-PnL contribution divided by total positive net-PnL contribution across comparable buckets. This avoids cancellation by losing buckets.
- Neighborhood tests perturb each mutable numeric strategy parameter separately by −10% and +10%; system invariants are not mutable parameters.
- PSR uses weekly UTC returns. `daily-psr-dsr-v1` uses aligned daily return series to estimate correlation among evaluated trials for DSR adjustment; it does not change the weekly PSR input.
- OOS remains sealed until the core begins the single authorized OOS run. Agent profiles never receive its path or credentials.

## 12. State machine and approvals

```text
DRAFT -> DEVELOPMENT_RUNNING
  | technical failure -> RUN_ERROR / DATA_BLOCKED -> idempotent rerun
  | development fail -> ANALYSIS_READY -> MUTATION_PROPOSED -> new version -> DEVELOPMENT_RUNNING
  | development pass -> OOS_RUNNING

OOS_RUNNING
  | fail -> REJECTED
  | pass -> READY_FOR_PAPER

READY_FOR_PAPER
  | explicit user approval -> PAPER_RUNNING

PAPER_RUNNING
  | daily loss stop -> PAPER_DAILY_PAUSED -> automatic next-UTC-day resume
  | MDD stop -> PAPER_MDD_HALTED -> explicit user approval -> PAPER_RUNNING
  | unreconciled data gap -> PAPER_DATA_GAP -> repair + explicit user approval -> PAPER_RUNNING
```

Agents may request a transition. Only the Python core validates preconditions and commits it transactionally.

An explicit approval must:

- come from an allowlisted user identity through the core CLI/API or an authenticated Buzz event;
- name the action and exact object hashes;
- be recorded with source event/command hash, timestamp, and idempotency key;
- be rejected if ambiguous, stale, duplicated with different payload, or for a superseded version.

The system must not depend on Buzz's native workflow-approval feature. The core interprets an authenticated, narrowly formatted Buzz command and remains the approval authority.

## 13. Agent control plane

### 13.1 Runtime pattern

Use one Hermes Orchestrator profile and three independent Hermes worker profiles:

- `orchestrator`
- `research`
- `coding`
- `analysis-ledger`

Each worker is invoked as a fresh one-shot job with a strict JSON envelope. The core chooses the allowed action and materializes an input bundle; the orchestrator launches the correct profile; the worker emits JSON plus referenced artifacts; the core validates schema, hashes, permissions, and state before committing anything.

Primary roles must not use plain `delegate_task`. Hermes delegation starts children with fresh context, applies one global child model configuration, and inherits parent tool access. Separate profiles provide independent model, credentials, skills, and state. Profile separation is not filesystem security, so OS account permissions or containers enforce data and credential boundaries.

### 13.2 Orchestrator

- Model: logical pinned name `deepseek/deepseek-v4-pro-0813`.
- Provider: `openrouter`, authenticated by `OPENROUTER_API_KEY`.
- Thinking enabled; reasoning effort `low`.
- No model fallback.
- Responsibilities: translate a core-approved action into a worker job, verify completion shape, and return the result handle.
- Forbidden: deciding validation outcomes, modifying strategy content, reading sealed OOS, approving paper, or controlling positions.

### 13.3 Research Agent

- Model: logical pinned name `deepseek/deepseek-v4-pro-0813`.
- Provider: `openrouter`, authenticated by `OPENROUTER_API_KEY`.
- No model fallback.
- Inputs: development artifacts, prior development ledger entries, immutable invariants, and public research sources.
- Output: exactly one falsifiable mutation hypothesis in the research schema.
- Forbidden: OOS access, code changes, direct database writes, approval requests disguised as results, or more than one mutation.

Deployment preflight must confirm that the exact model ID resolves through the configured provider. If it does not, the profile is unavailable; the system must not silently substitute another model.

### 13.4 Coding Agent

- Model: logical pinned name `deepseek/deepseek-v4-flash-0731`.
- Provider: `openrouter`, authenticated by API key.
- No model fallback.
- Workspace: a dedicated Git worktree for the assigned version.
- Skills:
  - Ponytail full mode.
  - `test-driven-development`.
  - `systematic-debugging`.
  - `verification-before-completion`.
  - `using-git-worktrees`.
- The full Superpowers bootstrap is not loaded in the headless worker because its interactive approval flow is incompatible with one-shot jobs.
- Output: patch/commit reference, changed-file manifest, invariant check, and test results.
- Forbidden: SQLite access, sealed OOS data, paper credentials, external messaging, dependency addition without an explicit job allowance, or unrelated refactoring.

Before adoption, the coding profile must pass 10 representative repository tasks with at least 90% acceptance: requested change only, invariants unchanged, tests passing, response schema valid, and no unapproved dependency.

### 13.5 Analysis/Ledger Agent

- Model: logical pinned name `deepseek/deepseek-v4-flash-0731`.
- Provider: `openrouter`, authenticated by `OPENROUTER_API_KEY`.
- Thinking enabled.
- No model fallback.
- Input: a core-validated result bundle only.
- Output: `analysis-ledger-v1` JSON and a Markdown draft.
- Forbidden: direct SQLite/wiki writes, changing metrics, declaring pass/fail independently, reading credentials, or proposing an OOS-driven mutation.

### 13.6 Model failure policy

Every job records the resolved provider/model/configuration and fingerprint when supplied. Provider or schema failure becomes `AGENT_ERROR`. Invalid JSON or artifact references are retried once with the same pinned model and job; a second failure stops the job without changing strategy state.

There is no automatic model or provider fallback. During an LLM outage, new agent jobs pause. The deterministic paper risk engine, data recovery, state machine, and critical alert outbox continue.

## 14. Deterministic engines

The following are ordinary Python modules, not LLM agents:

- market data ingestion and validation;
- bar aggregation and regime calculation;
- backtest and paper execution simulation;
- fees, funding, slippage, leverage, and liquidation-safety accounting;
- risk and account management;
- metric calculation, bootstrap, PSR, and DSR;
- pass/fail gates and budgets;
- state transitions and approval validation;
- authoritative persistence and publication outbox.

This boundary prevents an agent from changing a result through prose, tool choice, or prompt interpretation.

## 15. Ledger and wiki

### 15.1 Ledger content

For each completed evaluation, the Analysis/Ledger Agent prepares evidence-backed content containing:

- executive summary;
- failed and achieved gates;
- loss attribution by symbol, direction, regime, six-month period, exit reason, and cost type;
- comparison with the previous version;
- run and trade references for every material claim;
- exactly one mutation hypothesis for development failures only.

OOS failure analysis may explain evidence but must contain no mutation proposal and must not be routed to Research or Coding profiles.

Ledger revisions append. They never overwrite the original agent output or prior rendered page. Paper trading uses a separate ledger namespace from backtests.

### 15.2 Publication

The core emits a versioned Markdown/JSON bundle. A separate private, read-only static wiki deployment consumes that bundle. It has no trading database credentials.

A wiki failure is non-blocking for backtest and paper risk processing. The outbox state becomes `PUBLISH_PENDING` and retries:

1. immediately;
2. after 5 minutes;
3. after 15 minutes;
4. after 60 minutes;
5. every 6 hours thereafter.

After 24 hours, send a Buzz warning and continue retrying. Each outbox row records at least `attempt_count`, `next_attempt_at`, and `published_revision_id`. Publishing is idempotent by revision ID.

Buzz holds discussion, user notes, approval commands, and notifications. It is not the ledger source of truth.

## 16. Paper engine

Paper uses the exact registered strategy, regime, position sizing, TP/SL, risk, and state-machine code exercised in backtest. Only its clock, market feed, and fill adapter differ.

- Entries require fresh required streams and a legal rounded order.
- Fill prices may use real-time book and trades but may never be more favorable than observable executable prices after configured latency and fees.
- Optional liquidation, long/short, and open-interest streams are tagged; their absence cannot create a signal.
- Daily-loss and MDD controls execute without an LLM.
- Starting paper, resuming after MDD, and resuming after an unreconciled data gap require explicit approval.
- Daily-loss pause alone resumes at the next UTC day.

Exchange testnet is postponed until after paper proves the internal lifecycle. Its later purpose is exchange authentication, order formatting, cancellation, reconnect, and reconciliation—not profitability validation.

## 17. Error recovery and atomicity

- State transitions and budget consumption occur in one SQLite transaction.
- Large artifacts write to a temporary path, are hashed, and atomically renamed before their database reference commits.
- A run is idempotent by its immutable input tuple; recovery may reuse verified completed artifacts but never assume an unknown external side effect succeeded.
- A deterministic technical failure becomes `RUN_ERROR` or `DATA_BLOCKED` and may rerun without consuming selection or OOS budget.
- An invalid agent response is retried once; then `AGENT_ERROR`.
- Paper recovery reconciles local intents, observed market events, and the simulated account chronologically before enabling entries.
- Critical alerts are emitted from a deterministic transactional outbox and do not depend on an LLM response.

## 18. Minimum verification suite

The MVP is not complete without automated checks for:

1. deterministic golden-result hashes for a fixed dataset, strategy, and policy;
2. cross-asset rule and parameter equality;
3. 0.5% per-position and 2.0% aggregate planned-risk invariants;
4. 2x isolated leverage and gross leverage ceiling;
5. no-lookahead at 1-hour signal, daily regime, funding, and macro publication boundaries;
6. fees, slippage, funding direction, quantity rounding, and minimum order handling;
7. conservative same-minute TP/SL ambiguity;
8. walk-forward, bootstrap seed, PSR, DSR, neighborhood, leave-one-out, and every gate boundary;
9. exact 15% MDD rejection and halt behavior;
10. OOS sealing, one-run budget, rejection, and explicit new-family retry approval;
11. `READY_FOR_PAPER` not starting without exact approval;
12. daily pause auto-resume versus MDD/data-gap manual resume;
13. invalid/duplicate/stale approval rejection;
14. data gap, duplicate, time reversal, disconnect, REST recovery, and reconciliation fault injection;
15. agent schema, capability, credential, and artifact-boundary violations;
16. end-to-end data → backtest → validation → ledger → publish bundle;
17. wiki retry schedule and idempotent revision publication.

No exchange testnet is required for this suite.

## 19. Deployment and security constraints

- Secrets are held per Hermes profile and core service identity; they never enter job envelopes or artifacts.
- The paper service has no live exchange trading credentials.
- Research and Analysis profiles are read-only against their materialized input bundles.
- Coding receives a disposable worktree and no database, OOS, paper, or messaging credentials.
- Network egress is allowlisted by role where the runtime supports it.
- Profile `SOUL.md` and prompts are guidance, not security controls.
- Approval identities, policy changes, and model configuration changes are audited.
- Updating a validation or execution policy creates a new policy hash and cannot retroactively rewrite old results.

## 20. Source interpretation

`initial_idea.md` and its links supplied the original concept. The final decisions in this specification deliberately narrow it:

- The two linked X posts remain inspiration only because their contents were not reliably retrievable for verification.
- Grok Bot is not part of the selected architecture or model roster.
- Buzz is used as a collaboration and approval-intake surface, not the authoritative trading database or wiki.
- Hermes is used for one orchestrator profile and three isolated specialist profiles. The deterministic core remains in charge of all financial and validation decisions.

Reference material:

- [Block Buzz repository](https://github.com/block/buzz)
- [Hermes Agent documentation](https://hermes-agent.nousresearch.com/docs)
- [Hermes Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
- [Hermes AI Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Hermes Buzz integration](https://hermes-agent.nousresearch.com/docs/integrations/buzz)
- [Hermes delegation behavior](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [xAI Grok Bot overview](https://docs.x.ai/grok-bot/overview)
- [Original X reference 1](https://x.com/antpalkin/status/2085431604906766385)
- [Original X reference 2](https://x.com/milesdeutscher/status/2090495296765821101)

## 21. Acceptance of this specification

After user review and explicit approval, this file becomes the authoritative architecture baseline. Implementation planning may decompose it into phases, but may not weaken its risk, OOS, approval, data-causality, or agent-capability boundaries without a new explicit design decision.
