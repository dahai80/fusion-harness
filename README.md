# fusion-harness

Canonical agent harness kernel for the Fusion ecosystem: budget funnels, lifecycle
hooks, permission engine, and context compaction. Pure stdlib, zero dependencies.

## Consumers

- **fusion-agent-studio** and **fusion-cowork** import this package directly
  (hard dependency — `pip install -e fusion-harness`; no in-tree fallback copies).
- **fusion-code** (TypeScript) keeps its own implementation held to the same
  [kernel contract](docs/contract.md) by parity tests — cross-language runtime
  sharing is not the goal; contract conformance is.

The behavior contract for all runtimes lives in [docs/contract.md](docs/contract.md).

## Modules

- `fusion_harness.funnels` — `LLMFunnel`, `ToolFunnel`, `LifecycleBus` (budget enforcement + lifecycle events)
- `fusion_harness.permissions` — `PermissionEngine` (deny-first permission checks)
- `fusion_harness.hooks` — `HookEngine` (lifecycle hook registry + dispatch)
- `fusion_harness.compactor` — `Compactor` (context window compaction with retention)

## Security defaults (0.2.0, breaking)

Permission enforcement is **fail-closed by default**: a `ToolFunnel` without a
wired `PermissionEngine` denies every tool call. Headless/CI deployments opt out
explicitly:

```bash
export FUSION_FUNNEL_PERMISSIONS=off   # or FUSION_FUNNEL_REQUIRE_PERMISSIONS=0
```

Every call skipped this way logs `permission enforcement DISABLED via ...` — no
deployment is silently unarmed. See [docs/contract.md](docs/contract.md) §5 for
the full environment variable table.

## Install

```bash
pip install -e ".[test]"
```

## Usage

```python
from fusion_harness import Compactor, LLMFunnel, LifecycleBus, PermissionEngine, ToolFunnel
```

## Test

```bash
pytest tests/        # unit coverage per module: funnels, permissions, hooks, compactor
```

The suite is the executable form of the contract: dangerous-pattern denies,
layered rule precedence, fail-closed hook semantics, compaction levels, and the
enforcement default flip. Consumer repos' CI (agent-studio, cowork) is the
downstream detector for hidden passthrough assumptions.

## License

Same license as the fusion monorepo.
