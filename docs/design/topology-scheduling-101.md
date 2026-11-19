---
doc_id: fusion-harness/design/issue-101
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "设计文档 issue 101", en: "Communication topology scheduling for team runtime" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/design/topology-scheduling-101.md
---

# Design: Communication topology scheduling for team runtime (issue #101)

Role-Aware Communication Topology Scheduling (arXiv:2610.25851). Status:
design only — implementation after F1-F5 data-plane work settles; design
frozen first.

## Problem

team_runtime.py has a fixed member graph. Budget funnels gate message
sends but do not shape who talks to whom. A static org chart wastes tokens
(mesh) or starves roles (chain) regardless of task phase.

## Topology bank (data, not code paths)

Static candidates as declarative edge sets:

    TOPOLOGY_BANK = {
      "chain": edges = [(m[i], m[i+1])],
      "star":  edges = [(lead, m) for m in members if m != lead],
      "mesh":  edges = all pairs,
      "role_filtered": edges = pairs sharing the role tag,
      "lead_only": edges = [] (broadcast via lead summarization only)
    }

Each bank entry: `{name, edges_fn(member_roles) -> edge list, max_fanout}`.
Adding a topology = adding data, no engine changes.

## Per-step selector

At each step (or phase boundary), score each candidate:

    score(T) = w1 * role_fit(T) - w2 * est_token_cost(T)

- `role_fit`: fraction of edges connecting complementary roles
  (role tags from TeamDefinition).
- `est_token_cost`: sum over edges of message-cost estimate (last observed
  message size per member, heuristic counter).
- Hard constraint: topologies whose estimated step spend exceeds the
  remaining funnel budget are filtered out BEFORE scoring.

Tie-break: prefer the previous step's topology (hysteresis, avoids
churn). Selected topology persisted in step telemetry.

## Telemetry

Per step: `{step, topology, edge_sends: {edge: tokens}, skipped_edges}`
— feeds failure attribution (#93) for per-edge verdicts later.

## Boundary with budget funnels

The selector proposes; the funnel disposes. team_runtime consults the
selector for the send set, then every send still crosses the existing
funnel budget gate. The selector can never authorize a send the funnel
would deny.

## Offline simulation

Tests run recorded team transcripts through the selector with synthetic
role tags and message sizes; assert: budget-filter correctness, hysteresis
stability, role-fit preference, and that a star topology is chosen when
the budget is tight.

## Deliverable plan

1. Topology bank + selector as `team_topology.py` (no team_runtime edits
   until the F1-F5 data plane settles).
2. Telemetry events + offline simulation tests.
3. Then (separate PR): team_runtime wiring behind a flag.

## Open questions

- Phase detection (planning vs execution) — explicit host annotation vs
  inferred from message types? Start explicit.
- Does `lead_only` need its own budget accounting for lead summarization?
