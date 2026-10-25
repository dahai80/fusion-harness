# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Canonical agent harness kernel for the Fusion ecosystem: budget funnels, lifecycle hooks, permission engine, and context compaction. Extracted from fusion-agent-studio's N0 kernel (issue #357). Consumers: fusion-agent-studio (imports this package, falls back to its in-tree copy when absent), fusion-cowork, fusion-code.

**Hard constraint: pure stdlib, zero dependencies.** `dependencies = []` in pyproject.toml — do not add any. All imports must come from the Python standard library.

**Cross-repo parity:** fusion-agent-studio keeps local fallback copies of these modules. Changes here are the source of truth, but the fallback copies must stay in sync or the two implementations drift. When changing a module, mirror the change in fusion-agent-studio's fallback copy.

## Commands

```bash
cd /Users/dahai/fusion && source .venv/bin/activate   # shared monorepo venv — required first
cd fusion-harness

pip install -e ".[test]"     # install
pytest tests/                # all tests
pytest tests/test_kernel.py::test_permission_deny_first -v   # single test
ruff check .                 # lint (line-length 100)
ruff format .                # format
```

Tests are sync + async (`asyncio_mode = "auto"`); no network, no model loading needed.

## Architecture

Four modules, wired together by the consumer at construction time (the kernel itself has no global singleton):

- `funnels.py` — the runtime spine.
  - `LifecycleBus`: in-memory ring buffer of `LifecycleEvent`s (per-session), optional JSONL persistence, subscriber callbacks. Every other module emits through it.
  - `ToolFunnel`: async gate around every tool call. `admit()` fires `PRE_TOOL_USE` hooks → permission engine `evaluate()` → budget check; `run_tool()` wraps the executor and fires `POST_TOOL_USE` / `TOOL_DENY` on the way out. A denied call never reaches the executor.
  - `LLMFunnel`: token-budget gate for LLM calls, `pre_llm`/`post_llm` accounting, emits lifecycle events on budget thresholds.
- `permissions.py` — deny-first `PermissionEngine`. Rules live in three layers (`node`, `graph`, `global`), scanned node → graph → global, first match wins. Four modes: `default`/`plan`/`approved`/`bypass`. Un-armed engine (no mode, no rules) passes through; URL arguments pointing at loopback hosts only (`localhost`/`127.0.0.1`/`::1`) are exempted from the network-danger heuristic. Persists to `~/.fusion-agent-studio/permissions.json`.
- `hooks.py` — `HookEngine`: registry of callback hooks (`@hook` decorator collects into a module-level registry; `flush_decorator_registry()` moves them into the engine) and command hooks. `PRE_TOOL_USE`, `POST_TOOL_USE`, `TOOL_DENY` are **fail-closed**: a hook error on these events denies the tool call. Command hooks are disabled unless `FUSION_HOOKS_ALLOW_COMMAND=1`. Config at `~/.fusion-agent-studio/hooks.json`.
- `compactor.py` — `Compactor` with threshold levels derived from `CompactionConfig` buffers (warning/error/manual). Compact path: fire `compact_before` hooks → summarize (LLM via injected `llm_gateway`, i.e. fusion-mlx) with deterministic fallback → inject `RetentionList` header → emit lifecycle event. Deterministic-only fallbacks (`_smart_truncate`, `_hard_compact`, reactive strip) run when no gateway or MLX summarization fails; summaries persist via injected `memory_engine`.

Dependencies flow one way: funnels → permissions + hooks; compactor → hooks + lifecycle_bus. Permissions and hooks know nothing about funnels.

## Environment Variables (behavior switches)

| Variable | Effect |
|---|---|
| `FUSION_PERMISSION_MODE` | Arms the engine with a mode (empty = un-armed passthrough) |
| `FUSION_PERMISSION_PASSTHROUGH=0` | Turns un-armed passthrough into `ask` instead of `allow` |
| `FUSION_PERMISSION_A3=0` | Disables the loopback-URL exemption heuristic |
| `FUSION_FUNNEL_REQUIRE_PERMISSIONS=1` | ToolFunnel fails closed when no permission engine is wired in (default: warn + proceed) |
| `FUSION_HOOKS_ALLOW_COMMAND=1` | Enables command-type hooks (default: no-op with logged reason) |
| `FUSION_HOOK_MAX_MS` | Hook execution timeout budget, default 50ms |
| `FUSION_COMPACT_MLX=0` | Disables LLM summarization in the compactor (deterministic only) |

## Code Style

- 4-space indent multiples, no docstrings, logging on all non-trivial paths (modules use `logging.getLogger` — keep it).
- All funnel entry points are `async`; hooks and permission evaluation run inside that async context but are sync calls (compactor's MLX path uses `asyncio.run` internally).
- Public API is re-exported in `fusion_harness/__init__.py` — new public symbols go there too.
