---
doc_id: fusion-harness/plan-verification
product: fusion-harness
class: platform
entry: developer
doc_type: guide
title: { zh: "计划落地验证矩阵", en: "Plan Verification Matrix" }
audience: developer
status: draft
version: 0.1.0
updated: 2026-11-12
owner: "@dahai"
source_of_truth:
  - docs/evolution-plan-2026-11.md
---

# Plan Verification Matrix — evolution-plan-2026-11

Maps every acceptance criterion in `docs/evolution-plan-2026-11.md` to the
concrete tests that verify it. Status as of v0.9.1 (branch
`feat/acceptance-verification`). Full suite: 216 passed, ruff clean.

Legend: **kernel** = verified in this repo, binding. **consumer** = requires
wiring in a consumer repo (studio/cowork/agent-studio/mlx) — tracked there,
out of harness scope per the standing sweep constraint (harness only).

## P1-1 MCP tool transport (G1) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| scripted stdio server fixture: spawn, list, call | `test_phase1_convergence.py::TestMCPTransport::test_list_and_call_roundtrip` | PASS |
| Streamable-HTTP transport (0.10.0 scope completion) | `test_mcp_http_and_orchestrator.py::TestMCPHttp` (session header, roundtrip, fail-loud) | PASS |
| funnel namespacing `mcp__<server>__<tool>` | `::test_funnel_namespacing` | PASS |
| deny-path: denied `tools/call` never reaches the server | `test_acceptance_verification.py::TestMCPDenyPath::test_deny_rule_blocks_mcp_tool_before_invoke` | PASS |
| allow-path reaches client through funnel | `::TestMCPDenyPath::test_allow_path_reaches_mcp_client` | PASS |
| error result flagged; uninitialized fail-closed | `::test_error_result_flagged`, `::test_uninitialized_raises` | PASS |
| stdlib-only module constraint | `::test_stdlib_only_mcp_module` (AST scan) | PASS |

## P1-2 Real token counting (G5) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| thresholds/compaction triggers switch on counter identity | `test_acceptance_verification.py::TestCounterIdentityThresholds::test_cjk_counter_compacts_where_heuristic_does_not` | PASS |
| heuristic keeps legacy chars/4 behavior | `test_phase1_convergence.py::test_heuristic_matches_legacy_chars_over_4` | PASS |
| CJK fixture deltas within documented bounds | `::test_cjk_counter_counts_cjk_as_one`, `::test_cjk_delta_versus_legacy` | PASS |
| tool-call payloads counted | `::test_count_messages_includes_tool_calls` | PASS |
| compactor honors injected counter | `::test_compactor_uses_injected_counter` | PASS |
| mlx-side counter (consumer injects tokenizer) | protocol seam `CompactionConfig.token_counter`; real tokenizer is **consumer** (fusion-mlx) | seam kernel / impl consumer |

## P1-3 Compaction survival contract (G6) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| after ANY compact path, pinned system segments verbatim | `test_acceptance_verification.py::TestSurvivalContractMatrix::test_survival_all_levels` (warning/error/manual) + `::test_survival_reactive_strip` + `::test_survival_mlx_gateway_path` | PASS |
| task goal re-pinned when compacted away; not duplicated when present | `test_phase1_convergence.py::test_task_goal_re_pinned_when_compacted_away`, `::test_goal_not_duplicated_when_still_present` | PASS |
| degradation fails loudly (log error + re-insert, never silent) | verified inside matrix tests (re-insert path) + `test_system_rules_survive_hard_compact` | PASS |
| manifest introspection | `::test_manifest_introspection` | PASS |
| contract documented | `docs/contract.md` §4 | PASS |

## P1-4 Tool-result clearing (G11) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| clearing-before-summarize order (first deterministic stage) | `test_phase1_convergence.py::test_old_tool_results_cleared_recent_kept` + compactor stage order (`_clear_old_tool_results` runs before `_smart_truncate`) | PASS |
| stubs preserve identity (id, byte count) | `::test_stub_preserves_identity_and_bytes` | PASS |
| off-switch honored | `::test_clearing_disabled` | PASS |

## P2-1 Subagent runtime (G3) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| summary-only boundary (child events stay on child bus) | `test_phase2_structure.py::TestSubagent::test_summary_only_return_and_events` | PASS |
| tool allowlist narrows parent surface | `::test_tool_allowlist_narrows_parent_surface` | PASS |
| permission mode never widens | `::test_permission_mode_never_widens` + `test_acceptance_verification.py::test_child_engine_mode_never_widens` | PASS |
| per-subagent budget via TokenLedger (single ownership) | `test_acceptance_verification.py::test_budget_enforced_via_ledger_balance`, `::test_ledger_receives_scoped_entries` | PASS |
| child PermissionEngine inherits parent rules, deny-wins over spec allow | `::test_child_engine_deny_wins_over_spec_allow` | PASS |
| failure visible, spec validation | `test_phase2_structure.py::test_agent_failure_is_visible`, `::test_spec_validation` | PASS |
| consumer wiring (studio/cowork ladders) | **consumer** — orchestration stays consumer-side by design | consumer |

## P2-2 Resumable checkpoints (G4) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| save/latest/load roundtrip, seq, prune | `test_phase2_structure.py::TestCheckpoints::test_save_latest_load_roundtrip`, `::test_seq_increments_and_list`, `::test_prune_keeps_tail` | PASS |
| resume reproduces state, not effects | `::test_resume_preserves_state_not_effects` | PASS |
| SIGKILL mid-session, reopen, state intact | `test_acceptance_verification.py::TestCrashResilience::test_checkpoint_survives_sigkill` | PASS |
| committed effects not re-executed (receipt replay no-op) | `::TestCrashResilience::test_receipt_cache_replay_noop` | PASS |
| cowork DAG node-state binding | **consumer** (documented, not implemented — per plan) | consumer |

## P2-3 Skills + permission derivation (G2) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| parse + derive roundtrip | `test_phase2_structure.py::TestSkills::test_parse_and_derive` | PASS |
| deny-wins conflict | `::test_deny_wins_conflict` + `test_acceptance_verification.py::test_child_engine_deny_wins_over_spec_allow` | PASS |
| glob grants | `::test_glob_allow` | PASS |
| fail-closed load (missing name, unknown keys) | `::test_missing_name_rejected`, `::test_unknown_permission_key_rejected` | PASS |
| registry dedupe/lookup | `::test_registry_dup_and_lookup` | PASS |

## P2-4 Evaluation baseline (G10) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| baseline published (`bench/baseline.json`) | `bench/compaction_eval.py` + `bench/baseline.json` (score 1.0, tolerance 0.0) | PASS |
| CI fails on regression beyond tolerance | `tests/test_eval_baseline.py::test_compaction_future_questions_baseline` | PASS |
| baseline holds under Phase 3 changes | `test_phase3_surpass.py::test_baseline_holds_with_advisor` | PASS |
| MLX-path nightly benchmarks | **consumer** (fusion-mlx nightly, never PR CI) | consumer |

## P3-1 Cache co-design (G7) — kernel seam DONE

| Criterion | Verified by | Status |
|---|---|---|
| advisor can only raise keep window (protect prefix cache) | `test_phase3_surpass.py::test_advisor_raises_keep_rounds`, `::test_advisor_never_shrinks_below_config` | PASS |
| advisor failure falls back to config (no-op discipline) | `::test_advisor_failure_falls_back` | PASS |
| prefix-reuse retention benchmark vs engine | **consumer** (fusion-mlx implements `CacheAdvisor` + nightly bench) | consumer |

## P3-2 Memory binding (G9) — kernel seam DONE

| Criterion | Verified by | Status |
|---|---|---|
| persist/recall roundtrip with in-memory fake | `test_phase3_surpass.py::test_persist_and_recall_roundtrip` | PASS |
| no sink / source failure = logged no-op | `::test_no_sink_is_noop`, `::test_sink_failure_swallowed_visible` | PASS |
| just-in-time injection, bounded, after system prefix | `::test_inject_context_just_in_time`, `::test_inject_noop_when_no_match` | PASS |
| contract documented | `docs/contract.md` §15 | PASS |
| studio wires real fusion-memory binding | **consumer** | consumer |

## P3-3 Sandbox execution tiers (G8) — kernel, DONE

| Criterion | Verified by | Status |
|---|---|---|
| risk band -> tier mapping | `test_phase3_surpass.py::test_risk_mapping_table` + `docs/contract.md` §13 table | PASS |
| sandbox without runner = blocked (fail-closed) | `::test_sandbox_without_runner_blocks_fail_closed`, `::test_funnel_denies_sandbox_tier_without_runner` | PASS |
| sandbox tier routes via runner inside funnel | `::test_funnel_routes_destructive_via_runner`, `::test_sandbox_with_runner_routes` | PASS |
| explicit blocked tier; no policy = passthrough | `::test_explicit_blocked_tier`, `::test_funnel_passthrough_without_policy` | PASS |
| real macOS sandbox-exec runner | **consumer** (consumers provide `SandboxRunner`) | consumer |

## Cross-cutting invariants

| Invariant | Verified by | Status |
|---|---|---|
| `dependencies = []` (pure stdlib) | pyproject + `test_stdlib_only_mcp_module` AST scan | PASS |
| CI green (test/lint/docs validate) | GitHub Actions on this branch | PASS |
| contract.md updated for all new surfaces | `docs/contract.md` §9–15 + §13 tier table | PASS |

## Consumer-tracked items (out of harness scope)

1. fusion-agent-studio / fusion-cowork: subagent + checkpoint + skills wiring (Phase 2 exit clause).
2. fusion-mlx: real `CacheAdvisor` implementation + nightly MLX-path benchmarks.
3. fusion-mlx: production tokenizer injected via `TokenCounter`.
4. fusion-studio: fusion-memory `MemorySink`/`MemorySource` binding.
5. Consumers: real `SandboxRunner` (sandbox-exec/VM profile).
6. fusion-cowork: DAG node-state checkpoint binding.
