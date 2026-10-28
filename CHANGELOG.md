# Changelog

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
