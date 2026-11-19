---
doc_id: fusion-harness/design/issue-100
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "设计文档 issue 100", en: "Memory consolidation op set and forgetting regression axis" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/design/memory-consolidation-ops-100.md
---

# Design: Memory consolidation op set + forgetting regression axis (issue #100)

Mem0 (arXiv:2504.19413), A-MEM (arXiv:2502.12110), MemoryAgentBench
(arXiv:2507.05257). Status: design only — implementation starts after
review. Store ops belong to fusion-memory (cross-repo issue if needed);
this kernel owns the gate policy and the regression suite.

## Op set at the write gate

memory_link.py's write gate is binary today (write / quarantine). It
becomes a consolidation decision against retrieval neighbors:

    MemoryOp = ADD | UPDATE | DELETE | NOOP

- Candidates extracted from conversation deltas (the persist_compaction /
  store path already has the payload).
- For each candidate: retrieve top-k similar existing memories (via the
  injected MemorySink).
- Decision policy (deterministic default, LLM-replaceable):
  - near-duplicate (similarity >= 0.92) → UPDATE (merge into existing id)
  - contradicts existing (same subject, conflicting value) → UPDATE with
    supersede marker; the losing version is tombstoned, not deleted
  - candidate is a retraction of an existing memory → DELETE (tombstone)
  - no meaningful neighbor (similarity < 0.5) → ADD
  - otherwise → NOOP (do not write; log + event)
- Every decision emits `memory_consolidation` event with
  {op, candidate_id?, neighbor_ids, similarity, reason}.

## Retroactive-update guard (A-MEM evolution, bounded)

A new memory may trigger updates of neighbors (description/tags refresh)
— bounded to k=3 neighbors per write, one hop, audited in the same event
log, never touching tombstoned entries.

## Forgetting regression axis (MemoryAgentBench)

evals/memory_bench.py — four competencies as deterministic offline checks
over a recorded memory corpus (no live store, no LLM):

1. accurate retrieval: pinned fact survives consolidation round-trips
2. test-time learning: fact injected mid-run is retrievable after
3. long-range understanding: multi-hop facts stay linked
4. selective forgetting: retracted facts are NOT retrievable, unrelated
   facts are NOT forgotten (over-forgetting is a gate failure)

Gate: `assert_memory_gate(report)` — same pattern as compact_bench #72.

## Deliverable plan

1. MemoryOp enum + consolidation policy in memory_link (opt-in flag).
2. Tombstone convention (kernel-side metadata; store alignment via
   cross-repo issue to fusion-memory).
3. evals/memory_bench.py + regression fixtures + CI gate.

## Open questions

- Similarity source: the MemorySink's own ranking (fusion-memory) vs a
  kernel-side heuristic counter — start with sink ranking, fall back to
  exact/near-exact text match offline.
- DELETE semantics without store support: tombstone metadata only?
