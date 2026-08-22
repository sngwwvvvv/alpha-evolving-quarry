# Hermes profile operations

The trading core is deterministic. Hermes profiles are advisory workers only. They cannot decide gates, mutate risk controls, approve paper, or write SQLite.

Use four isolated profiles. Do not share a Hermes home, `.env`, memory, or gateway token across them. Do not use plain `delegate_task` for these roles: Hermes children inherit parent tools and a single child model.

| Profile | Logical model | Provider | Auth | Notes |
| --- | --- | --- | --- | --- |
| `orchestrator` | `deepseek-v4-pro` | `deepseek` | `DEEPSEEK_API_KEY` | Thinking on, reasoning effort `low` |
| `research` | `gpt-5.6-sol` | `openai-codex` | ChatGPT/Codex OAuth | Exact model must resolve at preflight |
| `coding` | `deepseek-v4-flash` | `deepseek` | `DEEPSEEK_API_KEY` | Disposable git worktree only |
| `analysis-ledger` | `kimi-k2.6` | `kimi-coding` | `KIMI_API_KEY` | Thinking on; result-bundle input only |

There is no automatic model or provider fallback. If the pinned model is unavailable, new agent jobs pause with `AGENT_ERROR`. Paper risk, data recovery, the state machine, and critical alerts continue.

## Secrets

- Hold keys in the profile `.env` or core service identity. Never copy them into job envelopes, JSON responses, or hashed artifacts.
- Research and analysis are read-only against materialized input bundles.
- Coding receives a disposable worktree and no database, OOS, paper, or messaging credentials.
- Profile `SOUL.md` is guidance, not a security control. OS account permissions or containers enforce data boundaries.

## Capability boundaries

- Orchestrator translates a core-approved action into a worker job and returns a result handle. It must not decide validation, edit strategy content, read sealed OOS, approve paper, or control positions.
- Research may receive development artifacts, prior development ledger entries, invariants, and public sources. It emits exactly one falsifiable mutation. OOS, paper, SQLite, and credentials are forbidden even under alternate keys.
- Coding implements that mutation in a disposable worktree (`disposable: true`, path, `version_id`). No SQLite, sealed OOS, paper credentials, or unapproved dependencies.
- Analysis/Ledger consumes a core-validated result bundle only (OOS bundles may be analyzed). It emits `analysis-ledger-v1` JSON plus a Markdown draft, with optional loss axes (`loss_drivers`, symbol/direction/regime/period/exit/cost). Development-failure analysis may include a single mutation object or null; OOS analysis must keep mutation null. It must not write SQLite/wiki, change metrics, declare pass/fail, or propose an OOS-driven mutation.

## Preflight

1. Research: resolve `gpt-5.6-sol` through the configured `openai-codex` OAuth provider. If the exact model ID does not resolve, mark the profile unavailable. Do not substitute another GPT model.
2. Coding: run 10 representative repository tasks. Adoption requires at least 90% acceptance on: requested change only, invariants unchanged, tests passing, response schema valid, and no unapproved dependency.

## Runtime contract

Each worker is a fresh one-shot job with schema `agent-envelope-v1`. The core pins `job_id`, profile, logical model, provider, and input bundle. Invalid JSON or artifact references retry once with the same pinned job; a second failure is `AGENT_ERROR` with no strategy-state mutation. Successful jobs record the resolved model fingerprint (provider, logical model, resolved id, thinking/reasoning config) without credentials.
