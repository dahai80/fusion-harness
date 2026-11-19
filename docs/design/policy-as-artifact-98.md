---
doc_id: fusion-harness/design/issue-98
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "设计文档 issue 98", en: "Declarative policy-as-artifact (Progent)" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/design/policy-as-artifact-98.md
---

# Design: Declarative Policy-as-Artifact (issue #98, Progent)

arXiv:2504.11703. Status: design only — implementation starts after review.

## Problem

permissions.py rules are host-owned state, not artifacts. Users cannot
ship a task-specific privilege policy with a run, diff it, or fork it with
checkpoint lineage.

## Rule DSL (JSON subset, stdlib-parseable, no eval)

    {
      "version": 1,
      "name": "readonly-refactor",
      "rules": [
        {"effect": "allow", "tools": ["read_*", "grep*"], "constraints": {}},
        {"effect": "deny",  "tools": ["shell_exec"],
         "constraints": [{"arg": "command", "op": "not_contains", "value": "rm "}]},
        {"effect": "ask",   "tools": ["apply_edit"],
         "constraints": [{"arg": "path", "op": "startswith", "value": "src/"}]}
      ],
      "fallback": "ask"
    }

- `tools`: glob patterns over tool names.
- `constraints`: argument predicates over a fixed op set (`eq`, `neq`,
  `contains`, `not_contains`, `startswith`, `matches_glob`, `in`, `lt`,
  `gt`). No regex, no code execution — `matches_glob` is fnmatch.
- `effect`: allow / deny / ask. Evaluation order: deny → ask → allow →
  `fallback` (default `ask`, fail-safe to human_gate).
- Composition: policies stack; a more specific policy's deny wins;
  per-task selection is a list of policy names resolved by the host.

## Artifacts, not state

- A policy is a versioned artifact (ArtifactStore ref + content hash).
- Checkpoint lineage forks carry the active policy set: CheckpointState
  metadata gains `active_policies: [ref, ...]`; fork() copies the list,
  so a child run inherits exactly the policy set it was spawned with.
- Diffable: `policy_diff(a_ref, b_ref)` — rule-level added/removed/changed.

## Interpreter

`policy_engine.py`: pure, stdlib, deterministic; adversarial test set
(wildcard escapes, constraint contradictions, empty-tool globs, oversized
policies). The engine never widens: intersection semantics with the
existing PermissionEngine — a Progent allow cannot override a permission
deny.

## Deliverable plan

1. DSL schema + validation, interpreter, adversarial tests.
2. Artifact attach/diff/fork wiring.
3. Contract section.

## Open questions

- Do constraints need cross-argument predicates (e.g. src AND dst in the
  same tree)? Defer to v2 if not proven necessary.
- Per-task selection syntax: names vs inline policy objects.
