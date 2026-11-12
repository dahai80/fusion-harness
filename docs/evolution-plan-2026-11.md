---
doc_id: fusion-harness/roadmap
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "能力补齐与超越计划", en: "Gap-Fill and Surpass Plan" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-12
owner: "@dahai"
source_of_truth:
  - docs/evolution-plan-2026-11.md
---

# fusion-harness Gap-Fill and Surpass Plan (2026-11)

Input: `~/fusion/insight/fusion-harness-evolution-2026-11.md` (industry
harness survey 2026-11-12, G1-G11 gap list, three-phase evolution, five
defense lines). This document turns that survey into an executable plan:
every item gets scope, interface shape, acceptance evidence, and the
defense line it must not break.

Plan skeleton below; sections filled per milestone batch.

## 0. Executive summary

Two moves, one principle.

**Gap-fill (Phase 1-2)**: the industry has converged on eight harness
facets; fusion-harness lacks four of them (MCP, skills, subagent
orchestration, checkpoint/resume) plus three cross-cutting defects (real
token counting, compaction survival contract, tool-result clearing).
These are table stakes — they do not differentiate us, they only keep us
eligible. They ship first, in dependency order, each behind an injectable
stdlib interface.

**Surpass (Phase 2-3)**: we do not out-feature Claude Code; we out-*trust*
it. Three bets where nobody in the survey competes:
1. **Reliability-first harness** — transactional outbox / idempotent replay
   / checkpoint-resume as a first-class contract (industry: mostly absent).
2. **Harness-engine cache co-design** — compaction points negotiate with
   fusion-mlx prefix cache so compaction stops destroying KV reuse
   (paper 2610.28577 made the problem explicit; no shipped harness solves it).
3. **Evaluation-driven minimalism** — every new mechanism must buy measured
   benchmark gain (mini-swe-agent lesson + paper 2610.27301 "future
   questions" method); the harness publishes its own compaction/permission
   regression baseline instead of shipping features on faith.

Principle: **platforms are eating context management** (compaction
primitives already in the API). Our long-term anchor is governance +
reliability, not context bookkeeping. Everything in this plan either
deepens governance/reliability or feeds the evaluation loop.

## 1. Gap register (from insight doc)

| # | Gap | Severity | Plan item | Phase |
|---|---|---|---|---|
| G1 | No MCP client support | red | P1-1 MCP tool transport | 1 |
| G2 | No skills mechanism | red | P2-3 SKILL.md + permission derivation | 2 |
| G3 | No subagent orchestration | red | P2-1 subagent runtime | 2 |
| G4 | No checkpoint/resume (session-log replay only) | red | P2-2 resumable checkpoints | 2 |
| G5 | Token count = chars/4 heuristic (CJK/tool-result distortion) | yellow | P1-2 real token counting | 1 |
| G6 | No compaction survival contract (rules can be compacted away) | red | P1-3 survival contract | 1 |
| G7 | Compaction breaks engine KV prefix cache | yellow | P3-1 cache co-design | 3 |
| G8 | No sandbox behind permissions | yellow | P3-3 sandbox execution tiers | 3 |
| G9 | No memory system (store_summary callback only) | yellow | P3-2 fusion-memory binding | 3 |
| G10 | No harness evaluation baseline | yellow | P2-4 eval baseline | 2 |
| G11 | Compaction treats tool results as text (clearing not implemented) | yellow | P1-4 tool-result clearing | 1 |

Already held (not gaps — invariants to protect): deny-first fail-closed
permission engine (3 layers, 4 modes, risk scoring), transactional outbox +
idempotency/retry/deadline, provider seam (ACP-compatible wire), pure
stdlib kernel + contract doc + consumer parity tests.

## 2. Phase 1 — convergence surface (M1-M4)

Goal: close the convergence-list gaps without adding dependencies and
without weakening any defense line. Ship order below is dependency order.

### P1-1 MCP tool transport (G1)

- **Scope**: stdlib MCP client over stdio + Streamable-HTTP transports
  (JSON-RPC is already solved by `provider/wire.py` patterns). Tool
  discovery (`tools/list`), invocation (`tools/call`), and capability
  filtering. MCP tools enter `ToolFunnel.admit()` like native tools —
  same permission engine, same budget accounting, same lifecycle events;
  an MCP tool gets no new privilege surface.
- **Interface**: `fusion_harness/mcp/client.py` + `MCPToolSource` adapter
  consumed by the funnel's tool registry. Server list injectable; no
  config file format invented — consumers pass connection specs.
- **Non-goals**: MCP server mode (exposing harness tools as MCP), sampling,
  roots.
- **Acceptance**: contract tests against a scripted stdio MCP server
  fixture (spawn, list, call, deny-path, budget-path); deny-first holds
  for MCP tools (a denied `tools/call` never reaches the server).

### P1-2 Real token counting (G5)

- **Scope**: replace chars/4 with an injectable `TokenCounter` protocol;
  default implementations: (a) `HeuristicTokenCounter` (current, kept for
  stdlib-only installs), (b) mlx-side counter via injected callable (the
  consumer already owns the tokenizer). CJK ratio and tool-result payloads
  covered by unit fixtures.
- **Interface**: `CompactionConfig.token_counter`, `LLMFunnel` accounting
  both accept the protocol — single seam, no engine dependency in-kernel.
- **Acceptance**: budget thresholds and compaction triggers switch on
  counter identity in tests; CJK fixture deltas within documented bounds.

### P1-3 Compaction survival contract (G6)

- **Scope**: define pinned segments that compaction must never drop or
  degrade: system rules, permission constraints, task goal, retention
  list header, active permission decisions. Define the post-compaction
  re-injection manifest (what Claude Code re-injects, adapted: pinned
  segments + recent file touches + skill descriptions once P2-3 lands).
- **Interface**: `RetentionList` gains `pinned` categories;
  `Compactor.compact()` validates the manifest post-compaction and fails
  loudly (visible failure, not silent truncation) if a pinned segment is
  degraded. Contract documented in `docs/contract.md`.
- **Acceptance**: property-style tests — after any compact path
  (deterministic or MLX), pinned segments present verbatim; paper
  2611.03223-style scenario (rule violation post-compaction) as a
  regression test.

### P1-4 Tool-result clearing (G11)

- **Scope**: implement Anthropic's "clearing tool results is the safest
  compression" as the first deterministic compaction stage: oldest tool
  results collapse to stubs (tool name, key identifiers, byte count)
  before any summarization. Bounded by retention window + pinned
  exceptions (e.g. last tool result per open thread).
- **Acceptance**: compaction traces show clearing-before-summarize order;
  stubs preserve enough identity for downstream interpolation checks
  (cowork parity test stays green).

Phase 1 exit: all four merged + released; consumer parity tests in
studio/cowork still green; no new `dependencies = []` violations.

## 3. Phase 2 — structural upgrade (M5-M8)

### P2-1 Subagent runtime (G3)

- **Scope**: child agent sessions with isolated context, scoped tool
  surface, scoped permissions, and summary-only return. Built on the
  existing `SUBAGENT_*` lifecycle events + `LifecycleBus` + provider seam
  (a subagent is a session on the `AdapterHost`, not a new execution
  model). Per-subagent budget ledgers via `TokenLedger(scope=...)`.
- **Interface**: `fusion_harness/subagent.py`: `SubagentSpec` (prompt,
  tool allowlist, permission mode, budget caps) + `SubagentRunner`
  (spawn, stream, collect summary, enforce caps). Orchestration ladder
  (background agents, teams, workflows) stays consumer-side — the kernel
  owns one subagent execution primitive, not a fleet framework.
- **Acceptance**: summary-only boundary proven by tests (child events do
  not leak into parent bus except the summary event); per-subagent budget
  caps enforced by `LLMFunnel` + `TokenLedger`; permission scoping
  inherited from parent engine with child-only tightening (child can
  narrow, never widen — mirrors `Deadline.child()` semantics).

### P2-2 Resumable checkpoints (G4)

- **Scope**: upgrade `SessionEventLog` replay into resumable checkpoints:
  durable checkpoint record at tool-call boundaries (pre-state: context
  digest, budget state, permission mode, outbox offsets), resume API
  rebuilding session state without re-executing committed tool effects
  (outbox receipts make effect re-execution idempotent — the two modules
  are designed to compose here).
- **Interface**: `fusion_harness/checkpoint.py`: `CheckpointStore`
  (sqlite, stdlib) + `resume(session_ref)` returning a rebuilt
  `SessionState`. Cowork DAG node-state binding is consumer work,
  documented not implemented.
- **Acceptance**: kill-mid-session test (SIGKILL between events), resume
  reproduces state, committed tool effects not re-executed (receipt
  replay no-op), uncommitted work re-runs exactly once.

### P2-3 Skills + permission derivation (G2)

- **Scope**: `SKILL.md`-style packaged skills (frontmatter + body, English
  source of truth, bilingual allowed) loaded as injectable context; from
  paper 2611.08905, derive per-subagent tool/workspace authorization from
  skill content — the permission engine's three layers are the landing
  spot for derived grants (derived rules are node-layer, never global).
- **Interface**: `fusion_harness/skills.py`: loader + `SkillManifest` +
  `derive_permissions(skill) -> PermissionRule list` (deterministic parser,
  not model-judged — Rule 5: decide with code).
- **Acceptance**: derived-rule tests incl. deny-wins-conflict; a skill
  declaring tool use outside its grant is rejected at load (fail-closed).

### P2-4 Harness evaluation baseline (G10)

- **Scope**: recorded-trajectory regression set for compaction and
  permission behavior using the "future questions" method (2610.27301):
  evaluate compacted context on questions that appear *after* the
  compaction point. Harness-side benchmark runs offline in CI
  (deterministic paths); MLX-path benchmarks nightly, never in PR CI.
- **Acceptance**: baseline published in repo (`bench/baseline.json`),
  CI fails on regression beyond documented tolerance; every Phase 3 item
  must show its benchmark delta here before merge — this is the
  minimalism gate (defense line 4).

Phase 2 exit: subagent + checkpoint + skills consumed by at least one
consumer repo (studio or cowork) — kernel code without a consumer wiring
is not done (standing completion rule).

## 4. Phase 3 — surpass bets (M9-M12)

### P3-1 Harness-engine cache co-design (G7)

- **Scope**: compaction points become cache-aware: the compactor asks the
  engine (fusion-mlx, injected callable) where natural prefix boundaries
  are and prefers compaction at stable boundaries so KV prefix reuse
  survives; explicit cache-stability metric exported per compaction.
- **Interface**: injectable `CacheAdvisor` protocol on `CompactionConfig`
  (stdlib kernel stays engine-free); fusion-mlx implements it.
- **Acceptance**: benchmark shows prefix-reuse retention after compaction
  vs current behavior; regression protected by P2-4 baseline.
- **Why it surpasses**: paper 2610.28577 names the problem; no shipped
  harness co-designs this. Fusion vertical depth (own engine) is the moat.

### P3-2 Memory binding (G9)

- **Scope**: compaction summaries persist to fusion-memory (UDS/HTTP
  11435, offline) and retrieve back into new sessions (just-in-time
  retrieval per Anthropic context-engineering guidance, not preload).
  Memory is state, not transcript replay (paper mainline 2).
- **Interface**: injectable `MemorySink`/`MemorySource` protocols;
  kernel never links fusion-memory directly.
- **Acceptance**: round-trip test with in-memory fake; contract doc
  updated; consumer (studio) wires the real binding.

### P3-3 Sandbox execution tiers (G8)

- **Scope**: permission engine output extended with an execution tier:
  `process` (default) / `sandbox` (macOS sandbox-exec / VM profile) /
  `blocked`. High-risk tools route to sandbox when the consumer provides
  a sandbox runner; `bypassPermissions` requires sandbox tier (aligning
  Claude Code semantics: bypass only inside isolation).
- **Interface**: `fusion_harness/sandbox.py` protocol + policy mapping in
  `permissions.py` (deny-first unchanged; sandbox is an enforcement
  mechanism, not a new policy engine).
- **Acceptance**: bypass-without-sandbox denied by default (test), runner
  injection test, risk-score-to-tier mapping table documented.

## 5. Defense-line invariants (apply to every milestone)

1. **Reliability layer survives every refactor** — outbox / idempotency /
   replay / session-log semantics are contract; breaking them fails review
   regardless of feature value.
2. **Permission fail-closed discipline** — deny-first, strictest-wins on
   conflicting decisions, bypass constrained by sandbox tier (P3-3). No
   convenience opt-outs beyond the existing documented env switches.
3. **Pure stdlib kernel + injectable seams** — new capabilities enter as
   protocols with consumer-side implementations (`dependencies = []`
   forever); consumer parity tests stay the CI detector.
4. **Evaluation gate** — a mechanism without a P2-4 benchmark delta is
   speculative; do not merge it (mini-swe-agent hedge).
5. **AGENTS.md compatibility** — instruction-file loading reads AGENTS.md
   alongside CLAUDE.md; no private instruction format.

## 6. Sequencing, dependencies, release mapping

```
P1-2 token counting ─┐
P1-3 survival contract ─┼─> Phase 1 release (0.7.0)
P1-4 tool clearing ─┘
P1-1 MCP transport ──> Phase 1 release or hotfix (independent)

P1-3 ─> P2-4 eval baseline ─> gates everything in Phase 2/3
P1-* ─> P2-1 subagent runtime ─> P2-3 skills+derivation (needs subagent scoping)
P2-2 checkpoints (needs outbox receipts — already landed #11/#13)
Phase 2 release (0.8.0)

P2-4 ─> P3-1 cache co-design ─┐
P2-2 ─> P3-2 memory binding ──┼─> Phase 3 releases (0.9.x)
P3-3 sandbox tiers (independent) ─┘
```

- Version mapping: minor bumps per phase (features), patch releases for
  gap-fixes inside a phase.
- Each plan item lands as one PR with: contract-doc update, tests per the
  acceptance block, benchmark entry when gated by P2-4, consumer-parity
  green. Kernel code without consumer wiring is not done.
- Risk register: MCP spec drift (pin tested spec revision in fixture);
  MLX-path benchmark flake (nightly only, tolerance documented); sandbox
  profile fragility on macOS upgrades (tier opt-in per consumer).
