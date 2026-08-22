PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS families (
    family_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_version_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES families(family_id),
    code_commit TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (strategy_version_id, family_id, code_commit)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES families(family_id),
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
    code_commit TEXT NOT NULL,
    data_snapshot_hash TEXT NOT NULL,
    derived_data_hash TEXT NOT NULL,
    validation_policy_hash TEXT NOT NULL,
    execution_policy_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (strategy_version_id, family_id, code_commit)
        REFERENCES strategy_versions (strategy_version_id, family_id, code_commit)
);

CREATE TABLE IF NOT EXISTS transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_hashes_json TEXT NOT NULL,
    source_command_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    family_id TEXT PRIMARY KEY REFERENCES families(family_id),
    performance_evaluated_versions INTEGER NOT NULL DEFAULT 0,
    oos_evaluations INTEGER NOT NULL DEFAULT 0,
    max_performance_evaluated_versions INTEGER NOT NULL DEFAULT 8,
    max_oos_evaluations INTEGER NOT NULL DEFAULT 1,
    CHECK (performance_evaluated_versions >= 0),
    CHECK (oos_evaluations >= 0),
    CHECK (performance_evaluated_versions <= max_performance_evaluated_versions),
    CHECK (oos_evaluations <= max_oos_evaluations)
);

CREATE TABLE IF NOT EXISTS paper_state (
    family_id TEXT PRIMARY KEY REFERENCES families(family_id),
    status TEXT NOT NULL,
    payload_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PUBLISH_PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    published_revision_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS strategy_versions_no_update
BEFORE UPDATE ON strategy_versions
BEGIN
    SELECT RAISE(ABORT, 'strategy versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS strategy_versions_no_delete
BEFORE DELETE ON strategy_versions
BEGIN
    SELECT RAISE(ABORT, 'strategy versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS runs_no_update
BEFORE UPDATE ON runs
BEGIN
    SELECT RAISE(ABORT, 'runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS runs_no_delete
BEFORE DELETE ON runs
BEGIN
    SELECT RAISE(ABORT, 'runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS transitions_no_update
BEFORE UPDATE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS transitions_no_delete
BEFORE DELETE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions are append-only');
END;
