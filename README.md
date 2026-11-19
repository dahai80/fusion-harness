---
doc_id: fusion-harness/readme
product: fusion-harness
class: platform
entry: developer
doc_type: readme
title: { zh: "fusion-harness 总览", en: "fusion-harness" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
---

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
Architecture decisions are recorded as ADRs; see
[ADR 0001](docs/adr-0001-composition-unit.md): the composition unit stays
service-level, in-process everything-is-a-plugin is rejected, and the kernel
commits to three adopted disciplines (event log, segmented pipeline, fail-closed
approval).

## Modules

- `fusion_harness.funnels` — `LLMFunnel`, `ToolFunnel`, `LifecycleBus` (budget enforcement + lifecycle events)
- `fusion_harness.permissions` — `PermissionEngine` (deny-first permission checks) + `ConfirmationPolicy` tiers and the deterministic `score_tool_risk()` bands, evaluated inside the engine (issue #9)
- `fusion_harness.hooks` — `HookEngine` (lifecycle hook registry + dispatch)
- `fusion_harness.compactor` — `Compactor` (context window compaction with retention)
- `fusion_harness.pipeline` — `ToolPipeline` five-stage segmented tool execution: pre-execute waterfall, identity-protected monotonic guards, one-shot fail-closed approval, around-dispatch timeout/retry, post-execute waterfall — ending in a frozen `ToolResult` (issue #5)
- `fusion_harness.session_log` — `SessionEventLog` durable append-only, schema-versioned, replayable session event log; live `LifecycleEvent`s are structurally rejected (issue #4)
- `fusion_harness.provenance` — `ProvenanceTracker` CaMeL-style taint tags on tool results; outbound tainted args gated narrow-only after the permission engine (issue #97)
- `fusion_harness.policy_engine` — `PolicyEngine` declarative policy-as-artifact (Progent rule DSL); deny-wins stack composition, checkpoint lineage inheritance, `policy_diff` (issue #98)
- `fusion_harness.consolidation` — `Consolidator` proactive FOLD/DROP/REWRITE verbs (reversible fold, retention-list-safe drop, bounded rewrite) (issue #99)
- `fusion_harness.memory_consolidation` — `MemoryConsolidator` ADD/UPDATE/DELETE/NOOP op set at the memory write gate; tombstone-based forgetting (issue #100)
- `fusion_harness.team_topology` — `TOPOLOGY_BANK` + `TopologySelector` per-step communication topology scheduling (role fit vs token cost under budget/fanout) (issue #101)
- `fusion_harness.team_runtime` — `TeamRuntime` lifecycle state machine, task board, mailbox; opt-in topology scheduling via `FUSION_TEAM_TOPOLOGY=1` (issues #31, #101)
- `evals/compact_bench.py` + `evals/memory_bench.py` — offline regression gates for compaction, consolidation, and memory forgetting

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

From PyPI:

```bash
pip install fusion-harness
```

From source (development):

```bash
pip install -e ".[test]"
```

Releases are published by CI on tag push (`.github/workflows/release.yml`) via
PyPI trusted publishing.

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
