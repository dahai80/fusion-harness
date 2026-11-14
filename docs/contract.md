---
doc_id: fusion-harness/architecture
product: fusion-harness
class: platform
entry: developer
doc_type: architecture
title: { zh: "内核行为契约", en: "fusion-harness Kernel Contract" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/contract.md
---

# fusion-harness Kernel Contract

Version: 1.0 (matches fusion-harness 0.2.0) — the behavior every runtime built on this kernel (Python consumers directly, the TypeScript implementation in fusion-code) must reproduce. Tests in this repo are the executable form of this contract.

## 1. Funnel Semantics

`ToolFunnel.admit()` is the single entry point for every tool call. Order IS the contract, deny-first:

1. **lifecycle** `tool.before` emitted
2. **hooks** `PRE_TOOL_USE` — a `block` decision denies immediately
3. **permission** (see §2) — `deny` denies, `ask` returns an outcome with `needs_approval=true` (the runtime owns the approval future)
4. **safety gateway** (legacy evaluator hook point) — `block` denies, `requires_approval` defers
5. **budget** — token budget exhausted denies
6. execute, then **lifecycle** `tool.after` + hooks `POST_TOOL_USE` / `TOOL_DENY`

Rules:
- A denied call never reaches the executor.
- Enforcement is **fail-closed by default** (since 0.2.0, issue #1): a funnel without a wired permission engine denies every tool call. Opt-out is explicit and loud (see §5).
- `LLMFunnel` applies the same budget gate to LLM calls (`pre_llm`/`post_llm` accounting, lifecycle events on threshold crossings).
- `LifecycleBus` is the event spine: per-session ring buffer, optional JSONL persistence, sync subscriber callbacks.

## 2. Permission Engine

- **Deny-first**: a deny anywhere in the chain wins immediately.
- Evaluation order: `BYPASS` mode short-circuit → **plan gate** → **content scan** → **rules** (layers scanned `node` → `graph` → `global`; first `deny` wins; `ask` beats `allow` regardless of layer; `path_scope` restricts a rule to tool args whose paths live under the scope) → readonly auto-allow → **loopback auto-allow** → mode policy → default `ask`.
- Modes: `default` / `plan` / `approved` / `bypass`. `plan` denies every non-readonly tool (first-class mode, not just a ctx flag).
- **Content scan**: 12 dangerous patterns (`rm -rf /`, `DROP TABLE`, `mkfs`, `sudo rm`, `curl|sh`, `wget|sh`, destructive `rmtree`, `chmod 777 /`, `dd of=/dev/`, fork bomb, `git push --force`, `eval`+`curl/wget`). Active in every mode except `bypass`. Count is contract — `test_dangerous_pattern_count_contract`.
- **Loopback auto-allow**: URLs targeting only `localhost`/`127.0.0.1`/`::1` are allowed without approval (local services). Disabled via `FUSION_PERMISSION_A3=0`.
- An un-armed engine (no rules, default mode) passes through with a loud warning, unless `FUSION_PERMISSION_PASSTHROUGH=0` turns passthrough into `ask`.
- Every decision is metered (`stats()`).

## 3. Hook Engine

- Event list: `PRE_TOOL_USE`, `POST_TOOL_USE`, `POST_TOOL_USE_FAILURE`, `SESSION_START`, `SESSION_END`, `STOP`, `ERROR`, `CHECKPOINT`, `TEAM_MESSAGE`, `PERSONA_RELOADED`, `COMPACT_BEFORE` (compactor).
- **Fail-closed events**: `PRE_TOOL_USE`, `POST_TOOL_USE`, `TOOL_DENY`. On these, a hook error OR the per-fire budget (`FUSION_HOOK_MAX_MS`, default 50ms) running out with hooks still unrun → `decision=block`. Skipping a security hook equals allowing the call — never silent.
- Non-security events swallow hook errors (`decision=approve`).
- Two hook types: `callback` (Python callable; `@hook` decorator registers into a module-level registry flushed at engine construction) and `command` (subprocess; **disabled unless `FUSION_HOOKS_ALLOW_COMMAND=1` — disabled command hooks return `block` on fail-closed events, not a no-op).
- Matcher semantics: `*` wildcard on tool name; unmatched hooks don't fire.

## 4. Compactor

- Levels derived from buffers: `error = window - error_buffer` (highest), `warning = window - warning_buffer`, `manual = window - manual_buffer`. `should_compact()` returns the highest crossed level.
- Pipeline (`compact(messages, level)`): fire `COMPACT_BEFORE` hooks + lifecycle → extract `RetentionList` (TODO lines, interface defs, constraints, pending tasks — kept verbatim) → microcompact (truncate oversized tool results, head+tail) → smart truncate (drop all but `keep_recent_rounds` rounds, summarize dropped) → hard compact at error level if still over → inject retention header.
- Summarization: LLM via injected gateway (fusion-mlx, async path `compact_async`) with **deterministic fallback** (`[Compacted prior context]` + user intents/tools/conclusions extraction). Fallback is always available; the gateway is an upgrade, never a dependency.
- System messages are never dropped. Retention list survives every level.
- **Tool-result clearing (0.7.0)**: before summarization, tool results older than `keep_recent_rounds` collapse to identity stubs `[tool result cleared: id=... bytes=...]` (disable: `CompactionConfig(clear_tool_results=False)`). Recent-round results keep head+tail truncation only.
- **Survival contract (0.7.0)**: every compaction path must (a) keep all pre-existing system messages verbatim — degradation is logged at ERROR and the message re-inserted; (b) keep the first user message reachable — if compacted away it is re-pinned as a `[Pinned task goal]` system segment (disable: `pin_task_goal=False`). `Compactor.survival_manifest(messages)` introspects pinned segments.
- **Token counting (0.7.0)**: `CompactionConfig.token_counter` accepts any object with `count_text(str) -> int`; default `HeuristicTokenCounter` (chars/4, unchanged). `CJKAwareHeuristicTokenCounter` counts CJK runs at ~1 token/char. `count_messages(messages, counter)` is the shared list counter.

## 5. Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `FUSION_FUNNEL_PERMISSIONS=off` | unset | **Opt-out of permission enforcement** (headless/CI). Every skipped call logs `permission enforcement DISABLED via ...`. |
| `FUSION_FUNNEL_REQUIRE_PERMISSIONS` | unset | `0/false/no/off/disabled` = same opt-out (legacy name). Any other value is ignored — enforcement is on by default. |
| `FUSION_PERMISSION_MODE` | unset | Arms the engine with a mode; un-armed engines pass through loudly |
| `FUSION_PERMISSION_PASSTHROUGH=0` | passthrough on | Un-armed engine asks instead of allowing |
| `FUSION_PERMISSION_A3=0` | heuristic on | Disables loopback-URL auto-allow |
| `FUSION_HOOKS_ALLOW_COMMAND=1` | unset | Enables command-type hooks |
| `FUSION_HOOK_MAX_MS` | `50` | Per-fire hook chain budget |
| `FUSION_COMPACT_MLX=0` | enabled | Disables LLM summarization (deterministic only) |

Breaking default change in 0.2.0: enforcement flipped from opt-in (`FUSION_FUNNEL_REQUIRE_PERMISSIONS=1` to enable) to opt-out. Operators running headless must set `FUSION_FUNNEL_PERMISSIONS=off` (or `FUSION_FUNNEL_REQUIRE_PERMISSIONS=0`).

## 6. Segmented Tool Pipeline (0.3.0, issue #5)

`ToolPipeline.run()` is an alternative admission+execution spine to `ToolFunnel.admit()` — segmented with ordering invariants:

1. **pre-execute** waterfall: `pre_hook` (rewrite args / raise `PipelineBlocked`), then `permission_engine.evaluate` (deny short-circuits before guards; ask sets approval-required)
2. **monotonic guards**: identity-protected — stored as an immutable tuple with no setter or extension API; run unconditionally after the waterfall; a guard deny is final; a raising or invalid-verdict guard fails closed
3. **one-shot fail-closed approval**: absent responder, unanswerable responder (`None` answer), or raising responder = deny, executor never invoked; never retried
4. **around-dispatch**: timeout (`timeout_s`) + retry (`retries`) around the executor (sync or async)
5. **post-execute** waterfall: accept / replace (return a new `ToolResult`) / block (raise `PipelineBlocked`)

Contract rules:
- The emitted final `ToolResult` is a frozen dataclass — mutation raises `FrozenInstanceError`.
- `run(approver=...)` is the per-call responder (overrides the instance attr); shared pipelines are safe under concurrent admits.
- `run(admission_only=True)` stops after stage 3 — verdict covers admission only, dispatch deferred to the consumer.
- `permission_engine.evaluate()` and `approver.request()` may return awaitables; the pipeline awaits them in place.
- A policy `deny` short-circuits before the guard stage (guards never observe denied calls).

## 7. Durable Session Event Log (0.3.0, issue #4)

Structural domain separation: **durable** events are `session_log.SessionEvent`; **live** events are `funnels.LifecycleEvent`. `SessionEventLog.append_event()` raises `TypeError` on anything that is not a `SessionEvent`.

- Append-only: monotonically increasing `seq`; non-monotonic append raises `LogSchemaError`. JSONL persistence opens in append mode and fsyncs per event.
- Every record carries `schema_version` (`SCHEMA_VERSION = 1`); `load()` rejects newer schemas (`LogSchemaError`) and corrupt lines.
- `replay()` reduces to `SessionState`: `messages`, `completed_tool_calls` (paired by `call_id`), `compactions`, `ended`, `event_count`.
- Crash mid-tool-call: a `tool_call` with no matching `tool_result`/`tool_error` is reported in `incomplete_tool_calls` — recorded explicitly, never dropped.
- Unknown event types are preserved on replay (logged, not reduced) — forward compatible.

## 8. Confirmation Tiers (0.3.0, issue #9)

## 9. Subagent Runtime (0.8.0, plan P2-1)

- One primitive, not a fleet framework: `SubagentRunner.run(spec, agent_fn, parent_tools, parent_mode)` returns a `SubagentResult` — summary/status/turns/tokens only; child events surface as `SUBAGENT_START`/`SUBAGENT_END` on the lifecycle bus.
- Child narrowing is monotonic: tool surface = parent ∩ spec allowlist; permission mode = stricter of parent and spec (strictness: bypass < approved < default < plan); budget/turn caps are additive. Violations flag status `capped`, never widen authority.
- Agent execution is an injected async callable — the kernel owns isolation and accounting, not orchestration ladders.

## 10. Resumable Checkpoints (0.8.0, plan P2-2)

- `CheckpointStore` (sqlite, WAL, `synchronous=FULL`): `CheckpointState` = messages + budget + permission mode + outbox offset; `save/latest/load/list/prune`.
- Resume contract: state rebuild reads a checkpoint; committed tool effects are never re-executed — the outbox receipt cache owns effect idempotency (checkpoint restore + redelivery is safe by construction).

## 11. Skills + Permission Derivation (0.8.0, plan P2-3)

- `parse_skill`: `---` frontmatter (name/description/permissions) + body; missing name, malformed or unknown permission clauses reject at load (fail-closed).
- `derive_permissions(skill)`: deterministic, engine-shaped node-layer rules; deny clauses always win (strictest-wins); tool names map exact or trailing-`*` globs to anchored regexes.

## 12. Evaluation Baseline (0.8.0, plan P2-4)

- `bench/compaction_eval.py`: deterministic "future questions" compaction scoring (2610.27301) — no LLM in CI; `bench/baseline.json` records scores with tolerance; regression beyond tolerance fails the suite. Mechanisms without a benchmark delta here do not merge (minimalism gate).

`ConfirmationPolicy(tier, risk_threshold, tool_overrides)` with tiers `always` / `never` / `risk` is evaluated **inside** `PermissionEngine` — one decision point, consumers must not stack local policy layers on top.

- Deterministic risk bands (never LLM-judged): destructive 0.9 > network 0.6 > write 0.3 > readonly 0.0 (`score_tool_risk()`).
- An explicit rule allow is never escalated; an explicit deny is never downgraded. `bypass` mode ignores the policy.
- `evaluate(can_confirm=False)` (non-interactive path) fail-closes an ask-tier outcome into a deny with a retry-sequentially reason.
- The engine decision carries `tier`/`risk`/`confirmation`; `FunnelOutcome.confirmation` exposes it to consumers.

## 13. Sandbox Execution Tiers (0.9.0, plan P3-3)

- `SandboxPolicy.decide(tool, args, risk)` maps the permission engine's risk score/band to a tier: destructive band and score >= threshold route to `sandbox`, explicit `tool_tiers` may map any tool to `blocked`.
- Fail-closed: `sandbox` tier with no runner wired is denied; `blocked` is denied. Routing happens inside `ToolFunnel.run_tool` between admission and execution — permission decisions themselves are unchanged.
- Risk band -> default tier mapping (documented per P3-3 acceptance):

| Input | Default tier |
|---|---|
| `tool_tiers[tool]` set | as mapped (process / sandbox / blocked) — wins first |
| score >= `risk_threshold` (default 0.9, destructive band) | sandbox |
| other known band (network / write / read) | process |
| no band, score below threshold | process |

  A sandbox decision with no wired runner degrades to `blocked` (fail-closed, never raw execution).

## 14. Cache Advisor (0.9.0, plan P3-1)

- `CompactionConfig.cache_advisor` exposes `suggest_keep_rounds(messages) -> int`; the compactor takes `max(config.keep_recent_rounds, suggested)` — advice can only reduce compaction aggressiveness (protect prefix-cache stability), never increase it. Advisor failure logs a warning and uses config.

## 15. Memory Binding (0.9.0, plan P3-2)

- `MemorySink.store(key, payload, metadata)` / `MemorySource.retrieve(query, limit)`; `MemoryBridge.persist_compaction` and `recall`/`inject_context` (just-in-time, bounded, inserted after the system prefix). Sink/source failures are logged no-ops — memory is an upgrade, never a dependency.

## 16. MCP Tool Transports (0.7.0 stdio, 0.10.0 HTTP)

- `MCPStdioClient` (line-delimited JSON-RPC over a spawned server process) and `MCPStreamHttpClient` (Streamable-HTTP: JSON-RPC POST, JSON or SSE response, `Mcp-Session-Id` continuity) expose the same surface: `start() -> list_tools() -> call_tool() -> stop()`.
- `MCPTool.funnel_name` is `mcp__<server>__<tool>`; `MCPToolSource` adapts one server's tools into the funnel registry. Permission rules match the namespaced names like any native tool — an MCP tool gains no new privilege surface, and a denied `tools/call` never reaches the server.

## 17. Orchestration Primitives (0.10.0)

- `BackgroundPool(runner).spawn(spec, agent_fn) -> Task` + `collect() -> [SubagentResult]`; `ParallelTeam(runner).run(specs, agent_fn) -> [SubagentResult]` fans out concurrently. Both are thin concurrency shapes over `SubagentRunner` — summary-only return, mode narrowing, and budget isolation are inherited, never re-implemented.
- One member failing never cancels siblings; failures come back as `status="failed"` results. Workflow/DAG orchestration stays consumer-side (fusion-cowork owns the DAG engine); anything needing model judgment stays consumer-side.

## 18. Security Intel Loop (0.10.1, issue #24)

- `RiskIntelClient(base_url, ...)` calls fusion-security `POST /api/v1/agent/risk-score` (wire contract: `schemas/agent-risk-score.v1.json` in fusion-security) and pushes the harness's own allow/deny back to `POST /api/v1/agent/decisions` (closed feedback loop).
- Opt-in: `ToolFunnel(security_intel=...)`. Unwired or unreachable endpoint = exact previous behavior (logged no-op — intel is an upgrade, never a dependency; harness keeps working offline).
- Hints are **advisory by default** — recorded in the `security_intel` lifecycle event only. `enforce_hints=True` turns an intel `deny` hint into a funnel denial. Harness remains the deny-first executor in all cases; the permission engine's decision is never overridden by intel.

## 19. Spawn Brief Contract + Effort Tiers (0.11.0, issue #26)

- `SpawnBrief` carries objective / output_format / tool_guidance / boundaries / token_budget / concurrency / depth; `validate_brief()` is a pure function and `brief_from_event()` raises typed `SpawnBriefError` on incomplete payloads — vague briefs are bugs, fail visibly. `depth <= 3` is an orchestration-ladder guard.
- `EffortTierConfig.resolve(estimated_tool_calls)` is deterministic (no model call): <=3 calls means 1 agent; 3-10 means 2-4 subagents with explicit responsibilities; >10 means 10+ split. Rules are config, not lead-prompt improvisation.

## 20. Artifact-Summary Dual Channel (0.11.0, issue #27)

- `ArtifactStore` writes full worker outputs to `<root>/<run_id>/` and returns lightweight refs; `SummaryInjector.inject_front()` places per-worker summaries + refs at the FRONT of the coordinator context (after the system prefix), idempotent on re-inject (Lost in the Middle: front placement, mid-context recall collapses).
- Deterministic `default_summarize` fallback; injectable `summarize` callable; summary schema driven by the P2 brief's `output_format`.

## 21. Playbook Structured Append Protocol (0.11.0, issue #28)

- `PlaybookOp` (add/update/delete, provenance episodes required, typed validation) applied append-first to an append-only op log; `render()` folds deterministically with tombstones (history never rewritten); `trace(entry_id)` returns all originating episodes. Targets ACE failure modes: brevity bias + context collapse (arXiv 2510.04618).

## 22. KV-Cache Discipline (0.11.0, issue #29)

- `assert_stable_prefix(turn_prompts)` flags turns whose prompt does not extend turn 0 byte-identically (lint-style CI hook).
- `check_append_only(before, after, compaction=False)`: context mutation must be append-only; rewrites only at explicit compaction boundaries.
- `mask_tools` / `ToolAvailabilityState`: mid-task tool availability changes are masking flags (`x-fusion-masked`) on an unchanged, same-order schema list — never add/remove (Manus 2025-07-18: dynamic tool-list mutation invalidates the KV prefix and induces schema violations).

## 23. Team Entity (0.12.0, issue #31, T1)

- `TeamDefinition` (name, namespace_key memory anchor with fixed tier "team", 1..N `TeamMember`, playbook flag, max_depth 1..3) validates pure and fails visibly via typed `TeamDefError`: duplicate member names, empty namespace_key, depth overflow, member budget exceeding lead budget (Deadline.child semantics). `TeamMember` names match `[A-Za-z0-9_-]+`.
- `TeamRuntime` lifecycle state machine: DEFINED -> ACTIVE to PAUSED and back -> DISBANDED (typed `TeamStateError` on illegal transitions; `pause()`/`resume()` first-class). All events flow on the LifecycleBus as `team.task.*` / `team.member.*` / `team.msg.*` / `team.lifecycle`.
- Task board: `TeamTask` status transitions are whitelisted (todo -> in_progress -> done; blocked requires non-empty blocked_by); `claim_task` is compare-and-set atomic — exactly one concurrent claimant wins. `run_member` enforces the SpawnBrief contract, stacks member constraints (tool intersection, stricter permission mode only, budget = min(member, brief)), and lands result summaries on the task board, never directly on the parent. One member failing never cancels siblings.
- Mailbox: `send`/`broadcast` persist per-member JSON inboxes; a message counts as delivered only when the file write succeeds (atomic tmp + rename); msg.type enum (text | plan_approval | shutdown_request | shutdown_response) is validated, semantics are not; inbox content is never auto-injected into anyone's context — injection timing is the consumer's call.
- `save_state`/`load_state` round-trip board + inboxes + lifecycle; `disband` cleans up inboxes per policy. **Zero model calls in any TeamRuntime API** (CI-greppable test).
