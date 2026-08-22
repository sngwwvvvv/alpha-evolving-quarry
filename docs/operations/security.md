# Security operations

The MVP is backtest and paper only. Live exchange trading, testnet order execution, and custody of real funds are out of scope. See also `docs/operations/hermes-profiles.md`.

## Secrets per profile

Hold credentials in the profile `.env` or core service identity. Never copy them into job envelopes, JSON responses, hashed artifacts, wiki bundles, or outbox payloads.

| Identity | Secret | Allowed use |
| --- | --- | --- |
| Hermes `orchestrator` | `DEEPSEEK_API_KEY` | One-shot dispatch of a core-approved job |
| Hermes `research` | ChatGPT/Codex OAuth | Read-only materialized development inputs |
| Hermes `coding` | `DEEPSEEK_API_KEY` | Disposable worktree only |
| Hermes `analysis-ledger` | `KIMI_API_KEY` | Core-validated result bundle only |
| Trading core | SQLite path / artifact root | Deterministic persistence |

Do not share a Hermes home, `.env`, memory, or gateway token across profiles. Profile `SOUL.md` and prompts are guidance, not security controls. OS account permissions or containers enforce data boundaries.

Research and analysis are read-only against materialized input bundles. Coding receives a disposable worktree (`disposable: true`) and no database, sealed OOS, paper, or messaging credentials.

There is no automatic model or provider fallback. If the pinned model is unavailable, new agent jobs pause with `AGENT_ERROR`.

## Paper has no live credentials

The paper service has no live exchange trading API keys, withdrawal rights, or testnet order credentials. Feeds and fills in this MVP use `FakeClock` / `FakeRestClient`. Optional liquidation, long/short ratio, and open-interest streams are analysis tags only.

Do not place Binance trading keys in the paper process environment. Market-data REST used later for gap repair must be read-only and separate from any trading key.

## Allowlisted egress

Network egress is allowlisted by role where the runtime supports it:

| Role | Egress |
| --- | --- |
| Trading core | None required for backtest/paper MVP. Later: allowlisted Binance REST for paper gap repair only |
| Wiki publisher | Private wiki ingest host only. No SQLite path, no exchange, no LLM |
| Orchestrator | Pinned DeepSeek endpoint |
| Research | Pinned `openai-codex` OAuth provider |
| Coding | Pinned DeepSeek endpoint; no database/wiki/paper hosts |
| Analysis | Pinned Kimi endpoint; result-bundle input only |
| Buzz | Approval-intake / notification host only; not the ledger |

Publisher tests use `FakeWikiSink`. Do not add `requests` / `httpx` / raw sockets to `trading_desk.publish` in this MVP.

## Policy-hash changes

`EvaluationPolicy.v2()` hashes the canonical YAML document. Updating `src/trading_desk/validation/policy_v2.yaml` (or an execution policy) produces a new `policy_hash`. Existing `ResultBundle` rows and run identities keep their original hashes. Policy edits cannot retroactively rewrite old results or unseal OOS.

Approval identities, policy changes, and model configuration changes must be audited (append-only `approvals` / `runs` / artifact fingerprints). Ambiguous, stale (>24h), superseded, or duplicate approvals are rejected.

## Wiki isolation

The wiki consumes versioned Markdown/JSON bundles only. It has no trading database credentials and must never open the core SQLite file. Publication is idempotent by `revision_id`. Failures leave `PUBLISH_PENDING` and retry; they do not halt paper or backtest.

Paper ledger pages use namespace `paper`; development/OOS pages use `backtest`.
