# Recovery operations

The trading core is fail-closed. Recover by replaying deterministic state, never by editing SQLite rows or hashed artifacts. Wiki and Buzz failures must not block backtest or paper risk processing.

## Principles

- State transitions and budget consumption commit in one SQLite `BEGIN IMMEDIATE` transaction (`transition()`).
- Result artifacts write to a temporary path, are hashed, and are atomically renamed before any database reference commits.
- A run is idempotent by its immutable input tuple (`family_id`, `strategy_version_id`, `code_commit`, data and policy hashes). Recovery may reuse verified artifacts and must not assume an unknown external side effect succeeded.
- `RUN_ERROR` and `DATA_BLOCKED` are technical failures. They may rerun in place without consuming selection or OOS budget.
- An invalid agent response retries once on the same pinned job, then `AGENT_ERROR`. Paper risk, data recovery, the state machine, and the outbox continue during an LLM outage.
- Paper recovery reconciles local intents, observed market events, and the simulated account chronologically before enabling entries.
- Critical alerts come from the transactional outbox. They do not depend on an LLM.

## Commands

Use the project venv and a workspace-local SQLite file. Do not point these at a live exchange.

### Inspect state

```text
python -c "from trading_desk.state.db import Database; db=Database('state/trading_desk.sqlite3'); print(db.get_budget('<family_id>')); print(list(db.list_transitions('<run_id>')))"
```

### Replay due outbox (wiki / alerts)

Immediate, then 5m / 15m / 60m / every 6h. After 24h a `buzz_warning` row is inserted and retries continue.

```text
python -c "from trading_desk.state.db import Database; from trading_desk.state.outbox import claim_outbox_due; from trading_desk.publish import FakeWikiSink, process_publish_outbox; db=Database('state/trading_desk.sqlite3'); print(process_publish_outbox(db, FakeWikiSink()))"
```

Replace `FakeWikiSink` with the configured wiki sink in a real deployment. Never give the wiki SQLite credentials.

### Technical rerun (no budget)

From `RUN_ERROR` or `DATA_BLOCKED`, call `transition(db, run_id=..., to_state='DEVELOPMENT_RUNNING'|'OOS_RUNNING', idempotency_key=...)`. The state machine will not increment performance or OOS counters.

### Paper daily pause

Daily-loss pause auto-resumes at the next UTC day. No approval.

```text
python -c "from datetime import datetime, timezone; from trading_desk.state.db import Database; from trading_desk.state.transitions import transition; db=Database('state/trading_desk.sqlite3'); transition(db, run_id='<run_id>', to_state='PAPER_RUNNING', idempotency_key='resume-daily', now=datetime(..., tzinfo=timezone.utc))"
```

### Paper MDD or data-gap resume

Requires an allowlisted `ApprovalCommand` with exact object hashes. Data-gap also requires a real repair (`repaired=True` after chronological REST replay). Payload strings such as `"true"` are ignored.

1. `PaperEngine.repair_gaps()` (or equivalent REST replay) until `reconcile_chronologically()` returns OK.
2. `transition(..., to_state='PAPER_RUNNING', approval=..., repaired=True, allowlist=...)`.

Newer family versions reject resume as superseded.

### OOS rejection → new family

A rejected OOS family is terminal. Start a new family (`Database.create_family`) only after explicit user approval. Do not feed sealed OOS results into research or coding.

### Artifact reuse

If a hashed artifact already exists, `ArtifactStore.put_*` is a no-op for identical bytes and refuses collisions. Do not delete digest files to “force” a rerun.

## Do not

- Open the trading SQLite database from the wiki, Buzz, or an agent profile.
- Rewrite `strategy_versions`, `runs`, or `transitions` (append-only / immutable triggers).
- Resume paper while unreconciled, stale (`DATA_STALE` > 60s), or after an unapproved MDD/data-gap halt.
- Treat wiki `PUBLISH_PENDING` as a trading halt.
