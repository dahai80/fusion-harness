# fusion-harness

Canonical agent harness kernel for the Fusion ecosystem: budget funnels, lifecycle
hooks, permission engine, and context compaction. Pure stdlib, zero dependencies.

Extracted from fusion-agent-studio (N0 kernel, issue #357) so multiple consumers
(fusion-agent-studio, fusion-cowork, fusion-code) share one implementation instead
of four drifting copies.

## Modules

- `fusion_harness.funnels` — `LLMFunnel`, `ToolFunnel`, `LifecycleBus` (budget enforcement + lifecycle events)
- `fusion_harness.permissions` — `PermissionEngine` (deny-first permission checks)
- `fusion_harness.hooks` — `HookEngine` (lifecycle hook registry + dispatch)
- `fusion_harness.compactor` — `Compactor` (context window compaction with retention)

## Install

```bash
pip install -e .
```

## Usage

```python
from fusion_harness import Compactor, LLMFunnel, LifecycleBus, PermissionEngine, ToolFunnel
```

## Parity

`tests/test_parity.py` compares the kernel AST against the fusion-agent-studio
local fallback copies when that repo is available locally, guarding against drift.
fusion-agent-studio imports this package first and falls back to its in-tree copy
when the package is absent (CI-safe).

## License

Same license as the fusion monorepo.
