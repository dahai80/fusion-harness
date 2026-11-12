---
doc_id: fusion-harness/changelog
product: fusion-harness
class: platform
entry: developer
doc_type: changelog
title: { zh: "变更日志", en: "Changelog" }
audience: developer
status: draft
version: 0.7.0
updated: 2026-11-12
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

## 0.7.0 (2026-11-12)

### Phase 1 convergence surface (evolution plan P1-1..P1-4)

Gap-fill per `docs/evolution-plan-2026-11.md` — MCP transport, real token
counting seam, compaction survival contract, tool-result clearing. Pure
stdlib; additive; no behavior change without config.

- `fusion_harness.tokens` (P1-2): `TokenCounter` protocol +
  `HeuristicTokenCounter` (legacy chars/4, default) +
  `CJKAwareHeuristicTokenCounter` (CJK ~= 1 token/char); `count_messages`
  helper; `CompactionConfig.token_counter` injects a model-accurate
  counter into the compactor.
- Tool-result clearing (P1-4): deterministic compaction stage collapses
  tool results older than `keep_recent_rounds` to identity stubs
  (`[tool result cleared: id=... bytes=...]`) before any summarization —
  Anthropic's safest-first principle.
- Survival contract (P1-3): pinned segments survive every compaction path
  — system messages validated verbatim post-compaction (degradation is
  logged as error and re-inserted), task goal re-pinned as
  `[Pinned task goal]` when compacted away; `Compactor.survival_manifest()`
  introspection.
- `fusion_harness.mcp` (P1-1): stdlib MCP stdio client
  (`MCPStdioClient` initialize/tools-list/tools-call over JSON-RPC line
  wire) + `MCPToolSource` exposing tools as `mcp__<server>__<tool>` for
  the funnel's existing permission engine (deny-first unchanged; transport
  carries no privilege).

## 0.6.0 (2026-11-10)

### Provider seam: adapter contract + ACP-compatible wire (#15)

Pattern adoption from t3code (provider-core / provider-acp server seam) —
re-implemented stdlib-only, no upstream code vendored. One adapter per agent
runtime; the host is runtime-agnostic; the seam is a transport, not a second
policy engine (funnels / permission engine / compaction apply upstream,
unchanged).

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.provider.events`: UI-agnostic `SessionEvent` model +
  `EventBus` fan-out (stable wire event-type constants).
- `fusion_harness.provider.adapter`: `ProviderAdapter` abstract contract
  (start/stop/new_session/prompt/respond_permission/close_session) and
  `AdapterHost` — registry, id allocation, and the HiTL permission seam
  (`request_permission` is fail-closed: no handler or handler error = deny).
- `fusion_harness.provider.wire`: ACP-compatible JSON-RPC 2.0 codec,
  line-delimited (initialize / session-new / session-prompt /
  session-update notifications / request-permission round-trip).
- `fusion_harness.provider.session`: `SubprocessSessionRuntime` — spawns the
  adapter driver subprocess, lifecycle state machine
  (new/ready/running/awaiting_permission/closed), streaming notifications to
  events, driver permission requests routed through `AdapterHost`.
- Top-level exports: `AdapterHost`, `ProviderAdapter`, `SubprocessSessionRuntime`,
  `RuntimeNotReady`, `ProviderSessionEvent`, `ProviderSessionState`
  (aliased; plain `SessionEvent`/`SessionState` remain the session-log types).

Non-goals (unchanged from the issue): no t3code provider fleet, no source-control
integration, no hosted relay, 100% offline.

## 0.5.0 (2026-11-08)

### Cross-cutting concern ownership: deadline / retry / idempotency / metering (#13)

New public API (semver-minor: additive, no breaking changes), spec
`verification-infra-spec-1108.md` §3.4 — one ownership point per concern,
consumers opt in:

- `Deadline` / `DeadlineExceeded`: caller declares the deadline once;
  `child()` budgets only shrink; `check()` fails honestly at expiry
  (cow#378 dual-timeout fix).
- `RetryPolicy` + `run_with_retry()`: single retry mediation point —
  exponential backoff capped at `max_backoff`, clamped to the deadline;
  `coalesce_retry()` collapses stacked layer policies with a loud warning
  (as#615 / as#649 outage-window fix). `TransientError` is the marker base
  for retryable failures.
- `idempotency_key()` + `ReceiptCache`: canonical `(scope, node, action)`
  keys and a standalone receipt table; `OutboxStore` command receipts (#11)
  now delegate to `ReceiptCache` — single idempotency implementation.
- `TokenLedger`: append-only authoritative usage ledger; balance is always
  a recomputed sum, JSONL-persisted and rebuildable.
- `docs/ownership-map.md`: one-page ownership map with per-repo removal
  list for private timeout/retry/idempotency code.

## 0.4.0 (2026-11-08)

### Transactional outbox + idempotent command receipts + startup reconcile (#11)

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.outbox` (#11): stdlib-only SQLite (WAL, `synchronous=FULL`)
  transactional outbox — `OutboxStore.commit_step()` persists run events and
  side-effect enqueue in one transaction; `claim_next()`/`settle()` give
  serial at-least-once delivery with a lease and **persisted** `available_at`
  backoff; `record_command()`/`execute_command()` dedupe replays by
  `command_id` receipt (duplicate-issue/PR guard, cowork#381);
  `reconcile(orphaned_run_ids)` requeues expired leases and retires
  orphaned-run effects at boot (t3code `ProviderRuntimeRecoveryService`
  pattern). Design note: `docs/adr-0002-transactional-outbox.md`.


## 0.3.1 (2026-11-07)

### Docs-system v1.0 adoption (P1 pilot)

- Adopted the fusion docs-system v1.0 contract (grid position: platform x
  developer): Tier 0 (`docs/product-page.md`, `docs/get-started.md`),
  Tier 1 (`docs/api-reference.md`, `docs/config-reference.md`,
  `docs/architecture.md`, recipes), frontmatter contract headers on
  README/CHANGELOG/contract/ADR.
- `ci/validate_docs.py`: stdlib-only static validator (frontmatter
  completeness, doc-set inventory, source_of_truth paths, redline scan,
  line budgets) + `docs.yml` CI workflow gating markdown PRs.
- Validator messages/comments are English-only; English-only source
  constraint applied repo-wide (PR #10).

## 0.3.0 (2026-10-28)

### Segmented tool pipeline, durable session log, engine-level confirmation tiers (#4/#5/#9)

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.session_log` (#4): durable append-only `SessionEventLog`
  (schema-versioned frozen `SessionEvent` records, monotonic seq, JSONL +
  fsync persistence, `SessionEventLog.load()` restart reconstruction,
  `replay()` reducer with explicit `incomplete_tool_calls` for crash
  mid-tool-call). Live `LifecycleEvent`s are structurally rejected
  (`TypeError`) — domain separation, not convention.
- `fusion_harness.pipeline` (#5): five-stage `ToolPipeline` — pre-execute
  waterfall → identity-protected `MonotonicGuard`s (immutable tuple, deny is
  final) → one-shot fail-closed approval → around-dispatch (timeout/retry) →
  post-execute waterfall, ending in a frozen `ToolResult`.
- `PermissionEngine` confirmation tiers (#9): `ConfirmationPolicy`
  (always/never/risk) + deterministic `score_tool_risk()` bands evaluated
  inside the engine; `evaluate(can_confirm=...)` fail-closed on
  non-interactive paths; `FunnelOutcome.confirmation` carries the decision.
- `LLMFunnel.pre_llm` compaction success tracking (#7): `compact_after`
  payload carries `success` + error; failed compaction passes original
  messages through unchanged.

0.3.0 consumer-integration additions:

- `ToolPipeline.run(approver=...)`: per-call approval responder — shared
  pipeline instances stay safe under concurrent admits.
- `ToolPipeline.run(admission_only=True)`: stages 1-3 only, dispatch
  deferred (consumers that execute tool bodies elsewhere).
- `permission_engine.evaluate()` and `approver.request()` may be async
  (awaitables are awaited in place) — async funnel-backed policy adapters
  and interactive approval bridges work without wrappers.

Consumer adoption: fusion-agent-studio routes every tool admission through
`ToolPipeline` (single admission path, no side doors) and records every
session into a durable `SessionEventLog` — issues #4/#5 closed end-to-end.

## 0.2.2 (2026-10-27)

Features of #4/#5/#9 as initially released to PyPI (superseded by 0.3.0's
consumer-integration additions above).

## 0.2.1 (2026-10-26)

### PyPI release pipeline (#3)

- Published to public PyPI as `fusion-harness`. Tag pushes (`v*`) now run
  `.github/workflows/release.yml`: test → build (`python -m build` +
  `twine check`) → publish via PyPI trusted publishing (OIDC, no token).
- Consumers no longer need `pip install git+...` preinstall steps —
  fusion-agent-studio and fusion-cowork CI now install from PyPI.
- Added `.github/workflows/ci.yml`: ruff + pytest on Python 3.11/3.12/3.13
  for every push to master and every PR.

## 0.2.0 (2026-10-25)

### Breaking: permission enforcement fails closed by default (#1)

`ToolFunnel` previously passed tool calls through when no `PermissionEngine` was
wired (opt-in enforcement via `FUSION_FUNNEL_REQUIRE_PERMISSIONS=1`). The
default is now **deny** — an absent engine is treated as a wiring bug.

Migration for operators:

- Headless/CI deployments that relied on passthrough must opt out explicitly:
  `FUSION_FUNNEL_PERMISSIONS=off` (or the legacy name
  `FUSION_FUNNEL_REQUIRE_PERMISSIONS=0`).
- Every opt-out skip logs `permission enforcement DISABLED via <var> — headless
  deployment?` so the state is always visible in logs.
- Deny-first rules, the 12 dangerous-pattern matchers, and plan-mode gating are
  unchanged — only the default changed.

### Kernel consolidation (#2)

- `fusion-harness` is now a hard dependency of fusion-agent-studio and
  fusion-cowork; their in-tree `_local` fallback copies and `KERNEL_SOURCE`
  shims are deleted. Import failure is a loud `ImportError`.
- `docs/contract.md`: the kernel behavior contract (funnel order, permission
  modes, hook fail-closed rules, compaction levels, env vars) — the TypeScript
  implementation in fusion-code conforms to this contract via its own parity
  tests rather than sharing runtime code.
- Test suite grew from 5 smoke tests to 59: per-module unit coverage
  (funnels/permissions/hooks/compactor) including the fail-closed acceptance
  criteria from #1.
- Fixed: callback-hook exceptions on fail-closed events
  (`PRE_TOOL_USE`/`POST_TOOL_USE`/`TOOL_DENY`) now block instead of being
  swallowed (audit P-3 completion).

## 0.1.0 (2026-10-25)

Initial extraction from fusion-agent-studio N0 kernel (#357).
