# Changelog

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
