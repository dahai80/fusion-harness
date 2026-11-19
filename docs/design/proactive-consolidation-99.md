---
doc_id: fusion-harness/design/issue-99
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "设计文档 issue 99", en: "Proactive consolidation: MEM1 state rewrite plus AgentFold verbs" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/design/proactive-consolidation-99.md
---

# Design: Proactive Consolidation — MEM1 state rewrite + AgentFold verbs (issue #99)

arXiv:2506.15841 (MEM1), arXiv:2510.24699 (AgentFold). Status: design
only — implementation starts after review.

## Problem

compactor.py fires reactively on threshold. Proactive consolidation
edits context *before* saturation: per-turn state rewrite with a hard
cap (MEM1) and explicit FOLD/DROP verbs mid-trajectory (AgentFold).

## Verb set

- `FOLD(scope, summary)`: compress a resolved/dead-end episode into a
  brief summary. Scope = message range or round indices. Reversible:
  the original rounds move to the compaction archive (existing
  compaction_archive.py) and the fold is citable.
- `DROP(scope, reason)`: discard content entirely (useless entities,
  superseded turns). Requires a reason string; the dropped range is
  recorded, not reconstructed. DROP of retention-list items is refused.
- `REWRITE(state)`: MEM1-style per-turn merge — prior bounded state +
  new observation → new state under a hard token cap. When the merge
  exceeds the cap, the REWRITE is refused (caller falls back to FOLD).

## State-cap semantics

- `ConsolidationConfig(state_cap_tokens)` — hard cap; REWRITE above cap
  is rejected, never silently truncated.
- The bounded state lives as one system-role message marked
  `consolidation_state: true`; survival contract (issue #52) protects it
  like the retention header.

## Audit log (undo-safe)

Every verb appends to a JSONL consolidation log:
`{ts, verb, scope, summary_ref?, reason?, state_cap, actor}`. Folds are
undo-able by rehydrating the archive range; DROPs are logged but not
reversible (documented).

## Checkpoint composition

Fold points are natural checkpoint-fork points: applying a FOLD saves a
checkpoint first (lineage from checkpoint.py #74), so a fork can resume
from the pre-fold state.

## A/B gate extension

compact_bench gains a consolidation variant: fixtures get fold/drop
annotations; the gate scores state-cap compliance (never exceed),
marker/rule survival, and undo correctness (fold → rehydrate → original
text back). The seeded bad consolidator folds away pinned rules and must
fail the gate.

## Deliverable plan

1. Verb executor + audit log + state cap, wired behind
   `ConsolidationConfig.enable_proactive` (default off).
2. compact_bench consolidation axis.
3. Contract section.

## Open questions

- Who invokes the verbs — the model via tool-call surface, or the host
  loop? Start host-invoked; tool surface later.
- REWRITE summarizer: reuse compactor LLM gateway path with the refusal
  fallback (#88).
