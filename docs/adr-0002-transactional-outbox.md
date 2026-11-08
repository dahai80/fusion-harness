---
doc_id: fusion-harness/architecture
product: fusion-harness
class: platform
entry: developer
doc_type: architecture
title: { zh: "ADR 0002 事务性发件箱", en: "ADR 0002: Transactional outbox + idempotent command receipts in the kernel" }
audience: developer
status: stable
version: 0.3.2
updated: 2026-11-08
owner: @dahai
source_of_truth:
  - fusion_harness/outbox.py
---

# ADR 0002: Transactional outbox + idempotent command receipts in the kernel

Issue #11. Pattern source: t3code orchestration-v2 (`EventSink`, `EffectOutbox`,
`EffectWorker`, `ProviderRuntimeRecoveryService`), studied in
`insight/t3code-analysis-and-fusion-lessons-2026-11-08.docx`.

## Why the kernel

Live delivery pipelines lost runs to process restarts: in-memory run
registries die silently, resumes produce zombie children (cowork#372), and
retry-after-crash double-executes side effects (duplicate GitHub issues/PRs,
cowork#381). Crash consistency is cross-consumer (agent-studio / cowork /
code all ride this kernel), so it belongs here — stdlib-only, one module:
`fusion_harness/outbox.py`.

## Semantics mapping

| t3code | fusion-harness | Guarantee |
|---|---|---|
| `EventSink` one-transaction persist | `OutboxStore.commit_step(run_id, events, effects)` | Step events + side-effect enqueue commit in ONE SQLite transaction (WAL + `synchronous=FULL`) — kill -9 after commit loses nothing, before commit leaves no partial state |
| `EffectOutbox` + `EffectWorker` claim/execute/settle | `claim_next()` / `settle(ok, error, backoff_seconds)` | Serial at-least-once delivery; failed effects get a **persisted** `available_at` backoff, never an in-memory timer; a claim holds a lease (`lease_expires`) so a crashed worker cannot strand an effect |
| idempotent command receipts | `record_command(command_id, aggregate, fn)` | A replayed `command_id` returns the recorded result without re-running `fn` — the duplicate-issue/PR guard |
| `ProviderRuntimeRecoveryService` boot reconcile | `reconcile(orphaned_run_ids)` | Expired leases requeue in delivery order; effects of orphaned runs retire with an honest reason; the caller marks orphaned runs' terminal state on its own registry |

## Idempotency layers

1. **Command receipts** (`command_receipts`, PK `command_id`): whole-command
   dedupe. A re-run of "deliver issue X" with the same `command_id` is a no-op.
2. **Effect receipt keys** (`outbox.receipt_key`, UNIQUE, default
   `run_id:node_id:action`): enqueue-side dedupe — the same step effect
   committed twice inserts once.
3. **Consumer-side**: at-least-once delivery means the executor may still see
   an effect twice after a crash between execute and settle; consumers that
   need exactly-once pass their own `receipt_key` and check state before acting.

## Pilot consumer

fusion-cowork's workflow engine wires node side effects (github_api writes,
dispatch spawns) through `commit_step` — fusion-cowork#382. Boot reconcile
integrates with the cowork#372 zombie-resume fix.

## Rejected alternatives

- **Per-app reimplementation**: three consumers, three bugs (this is the
  cowork#381 root cause class).
- **Exactly-once delivery protocol**: overkill for side effects that are
  naturally idempotent-checked at the API layer (GitHub dedupes by content);
  at-least-once + receipts is the honest contract.
- **Non-SQLite store**: kernel is stdlib-only; sqlite3 WAL meets the durability
  bar for single-node local-first fusion.
