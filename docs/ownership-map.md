---
doc_id: fusion-harness/architecture
product: fusion-harness
class: platform
entry: developer
doc_type: architecture
title: { zh: "横切关注点所有权地图", en: "Cross-cutting concern ownership map" }
audience: developer
status: stable
version: 0.5.0
updated: 2026-11-08
owner: @dahai
source_of_truth:
  - fusion_harness/policies.py
---

# Cross-cutting concern ownership map

Issue #13. Spec: `~/fusion/architecture/verification-infra-spec-1108.md` §3.4 —
cross-cutting behaviors were re-invented per repo, and each invention produced
a new defect class. The kernel owns each concern once; consumers opt in.

## What the kernel owns

| Concern | Kernel API | Rule | Source defect |
|---|---|---|---|
| Timeout | `Deadline`, `DeadlineExceeded` | Caller declares the deadline ONCE; callees take `child()` budgets that can only shrink it. Libraries must not add private wrapper timeouts. | cow#378 (dual timeouts raced, wrong one won) |
| Retry | `RetryPolicy`, `run_with_retry`, `coalesce_retry` | One policy type; retries never stack across layers — `coalesce_retry()` keeps the outermost policy and drops inner ones with a loud warning. Backoff is clamped to the deadline: honest fail, never hang. | as#615 (hardcoded 120s LLM timeout), as#649 outage window |
| Idempotency | `idempotency_key`, `ReceiptCache` | Every external-write side effect carries a `(scope_id, node_id, action)` key; a replayed key returns the recorded result without re-running. | cow#381 (duplicate issues/PRs on re-run) |
| Metering | `TokenLedger` | Single authoritative append-only ledger; balance is always recomputed as a sum — mirror copies rebuild from the entry stream. | drift between in-memory counters and records |

## Composition rules

1. **Deadline propagation**: `deadline.child(seconds)` at every layer boundary.
   A callee that needs its own ceiling takes a child; a callee that never
   extends the caller is correct by construction.
2. **Retry mediation**: when a layer boundary would stack two policies
   (`coalesce_retry(outer, inner)`), the outermost survives and the inner is
   dropped with a warning. A 4-minute upstream outage must not become 16
   minutes of client-side waiting.
3. **Idempotency keys are mandatory for external writes**: GitHub writes,
   subprocess spawns, any non-replayable effect. The outbox (#11) enforces
   this via its `receipt_key`; `ReceiptCache` is the standalone form for
   consumers without a full outbox.

## Consumers and removal list

Per-repo private timeout/retry/idempotency code scheduled for replacement by
the kernel APIs:

- fusion-agent-studio: LLM call retry (issue #649 fix) — migrate to
  `RetryPolicy` + `run_with_retry`; hardcoded 120s LLM timeout (as#615) —
  migrate to `Deadline`.
- fusion-cowork: OutboxBridge effect receipt keys and delivery backoff —
  migrate key construction to `idempotency_key`; pipeline-level private
  retry wrappers (cow#378 class) — migrate to `coalesce_retry` mediation.

## Testing

`tests/test_policies.py` — 25 tests: deadline shrink-only children, honest
expiry, retry exhaustion/deadline clamping/stack collapse, receipt replay,
ledger rebuild-from-JSONL, outbox delegation.
