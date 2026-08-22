# Recovery operations

The trading core is fail-closed. Recover by replaying deterministic state, never by editing SQLite rows or hashed artifacts. Wiki and Buzz failures must not block backtest or paper risk processing.

## Principles

- State transitions and budget consumption commit in one SQLite `BEGIN IMMEDIATE` transaction (`transition()`).
- Result artifacts write to a temporary path, are hashed, and are atomically renamed before any database reference commits.
- A run is idempotent by its immutable input tuple (`family_id`, `strategy_version_id`, `code_commit`, data and policy hashes). Recovery may reuse verified artifacts and must not assume an unknown external side effect succeeded.
- Development technical failures are `RUN_ERROR` or `DATA_BLOCKED`. They may return only to `DEVELOPMENT_RUNNING` and consume no selection budget.
- A sealed-OOS technical failure stays `OOS_RUNNING` (the OOS budget is already consumed). Retry in place; do not invent `RUN_ERROR`/`DATA_BLOCKED` → `OOS_RUNNING` edges.
- An invalid agent response retries once on the same pinned job, then `AGENT_ERROR`. Paper risk, data recovery, the state machine, and the outbox continue during an LLM outage.
- Paper recovery reconciles local intents, observed market events, and the simulated account chronologically before enabling entries.
- Critical alerts come from the transactional outbox. They do not depend on an LLM.

## Commands

Use the project venv and a workspace-local SQLite file. Do not point these at a live exchange.

### Inspect state

```text
python -c "from trading_desk.state.db import Database; db=Database('state/trading_desk.sqlite3'); print(db.get_budget('<family_id>')); print(list(db.list_transitions('<run_id>')))"
```

### Replay due wiki outbox

Immediate, then 5m / 15m / 60m / every 6h. After 24h a `buzz_warning` row is inserted and retries continue. `process_publish_outbox` claims `topic=wiki_publish` only.

```text
python -c "from trading_desk.state.db import Database; from trading_desk.publish import FakeWikiSink, process_publish_outbox; db=Database('state/trading_desk.sqlite3'); print(process_publish_outbox(db, FakeWikiSink()))"
```

Replace `FakeWikiSink` with the configured wiki sink in a real deployment. Never give the wiki SQLite credentials. Alert rows stay on `claim_outbox_due(..., topic='alert')`.

### Technical rerun (development only)

Legal recovery edges: `RUN_ERROR → DEVELOPMENT_RUNNING` and `DATA_BLOCKED → DEVELOPMENT_RUNNING`. No budget is consumed.

```text
python -c "from trading_desk.state.db import Database; from trading_desk.state.transitions import transition; db=Database('state/trading_desk.sqlite3'); transition(db, run_id='<run_id>', to_state='DEVELOPMENT_RUNNING', idempotency_key='rerun-dev')"
```

### OOS crash

Do not `transition(..., to_state='OOS_RUNNING')` from `RUN_ERROR` or `DATA_BLOCKED` (those edges are illegal). If the run is already `OOS_RUNNING` after a technical failure, retry the same sealed evaluation:

```text
python -c "from trading_desk.state.db import Database; from trading_desk.storage.artifacts import ArtifactStore; from trading_desk.workflows.research_loop import run_sealed_oos; db=Database('state/trading_desk.sqlite3'); run_sealed_oos(db, ArtifactStore('artifacts'), run=db.get_run('<run_id>'), sealed=<sealed>)"
```

### Paper daily pause

Daily-loss pause auto-resumes at the next UTC day via `PaperEngine.maybe_resume_daily()`. No approval. Do not resume by calling `transition()` alone from an operator script unless the engine clock, flatten, and day-open hooks have already run.

### Paper MDD or data-gap resume

Use the paper engine, not a raw transition:

1. Chronological REST replay: `PaperEngine.repair_gaps()` until `reconcile_chronologically()` is OK.
2. MDD: `PaperEngine.resume_after_mdd(approval)` with action `resume_mdd`.
3. Data gap: `PaperEngine.resume_after_data_gap(approval, repaired=True)` with action `resume_data_gap`. `repaired` must be the bool `True` after a real repair; payload strings such as `"true"` are ignored.

Newer family versions reject resume as superseded.

### OOS rejection → new family

A rejected OOS family is terminal. The first family may use ungated `Database.create_family()`. After any family has `REJECTED`, further families must go through `approve_new_family` with action `approve_new_family` and object hashes `{rejected_family_id, proposed_family_id}`. Do not feed sealed OOS results into research or coding.

### Artifact reuse

If a hashed artifact already exists, `ArtifactStore.put_*` is a no-op for identical bytes and refuses collisions. Do not delete digest files to “force” a rerun.

## Do not

- Open the trading SQLite database from the wiki, Buzz, or an agent profile.
- Rewrite `strategy_versions`, `runs`, or `transitions` (append-only / immutable triggers).
- Resume paper while unreconciled, stale (`DATA_STALE` > 60s), or after an unapproved MDD/data-gap halt.
- Treat wiki `PUBLISH_PENDING` as a trading halt.
- Transition OOS crashes to `OOS_RUNNING` from development error states.
