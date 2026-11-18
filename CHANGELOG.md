---
doc_id: fusion-harness/changelog
product: fusion-harness
class: platform
entry: developer
doc_type: changelog
title: { zh: "变更日志", en: "Changelog" }
audience: developer
status: draft
version: 0.22.0
updated: 2026-11-18
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

## 0.22.0 (2026-11-18)

Host-hardening gap batch — issues #72-#76, plus legacy-pool fix #81.

### KV-cache hit-rate telemetry (issue #76)

- `CacheTelemetry` on `LLMFunnel`: cached vs uncached input split from
  provider usage payloads (OpenAI `prompt_tokens_details`, Anthropic
  `cache_read_input_tokens`, generic `cached_tokens`), per-run and
  aggregate hit rate, append-only prefix-stability counter with
  diverge-index break events, `prefix_break` lifecycle event.

### Checkpoint lineage fork (issue #74)

- `CheckpointState.lineage_id` + `parent_checkpoint`;
  `CheckpointStore.fork()` branches into a NEW lineage with an identical
  state copy — the original stays resumable untouched. `fork_chain()`
  walks root-first; `lineage_checkpoints()` queries the new column
  (guarded `ALTER TABLE` migration for pre-lineage stores); `prune()`
  never removes the lineage root.

### Host-owned destructive approval gate (issue #73)

- `destructive_gate.ApprovalGate`: table-driven destructive
  classification; tool-server confirmation fields never authorize;
  approval tokens ride the #66 queue, idempotent per call id,
  single-redemption, replay across call ids rejected with telemetry;
  `ToolFunnel` stage 2c denies destructive calls without a host grant.

### Memory write gate + artifact scanning (issue #75)

- `artifact_scan`: stdlib regex sets for prompt-injection and
  credential-exfiltration patterns, persistent queryable quarantine,
  untrusted-content stamping. `MemoryBridge` gates all memory writes
  (flagged content stored stamped `origin=untrusted`, never silently
  consolidated, never injected); `parse_skill` blocks flagged SKILL.md
  bodies at load via `SkillScanError`.

### Compact-Bench A/B eval gate (issue #72)

- `evals/compact_bench.py` + three recorded fixture transcripts: same
  task state scored full-history vs compacted against a deterministic
  context-availability stub (zero model calls). CI gate fails on
  task-success drop or rule-recall loss; seeded known-bad summarizer
  proves the gate can fail.

### Trajectory legacy-pool tolerance (issue #81)

- `_adapt_cowork` falls back to `member` / `workflow` node scope for
  legacy workflow-level events with empty `node_id`;
  `TrajectoryReader(skip_corrupt=True)` skips bad lines with a warning
  (default stays strict).

## 0.21.0 (2026-11-18)

HumanGate queue + JSON repair.

### HumanGate generic pending-question queue (issue #66)

- `HumanGate` doubles as a generic pending-question queue: `ask()`
  gains `requester` + `ttl_s` (lazy expiry), `resolve()` is a
  single-winner resolve that never raises, `list_questions()` filters by
  status/requester/session. Daemon exposes list/resolve; agents block on
  `wait()` (block policy) or poll `take_reply()` (continue policy).

### Stdlib JSON-repair helper (issue #67)

- `parse_repair(text)`: strict `json.loads` first, then a deterministic
  repair ladder — markdown fences, balanced-span extraction from prose,
  single- to double-quoted strings, trailing commas, truncated-output
  completion. Exhaustion raises `JSONRepairError`; no silent wrong data.

## 0.20.0 (2026-11-18)

Third multica-borrow batch — team run operations, issues #63-#65.

### Per-run event streams (issue #63, multica borrow F2)

- `TeamRuntime.run_member` records a per-run event stream via
  `session_log.py`: `run_started / prompt / tool_call / tool_result /
  tool_error / final / run_finished`, timestamped, one JSONL per run at
  `<state_dir>/runs/<task_id>/<attempt>.jsonl` (attempt increments per
  dispatch; the stream's session_id is the attempt number — task_id,
  member and team ride every event payload). A failed run replays
  completely, tool_error included.
- Redaction before write: sensitive key names (secret/token/password/
  api_key/authorization/credential/...) → `***`; embedded `KEY=value` /
  `KEY: value` patterns inside string values → value part `***`.
- `run_finished` carries usage straight from the funnel ledger balance
  (prompt/completion/reasoning/total — tolerance 0, no re-aggregation);
  session counter fallback when no ledger is wired.
- Plumbing is additive: `SubagentRunner.run(..., event_sink=None)`;
  `SubagentSession.log_event()` forwards to the sink and is a no-op
  without one — the plain subagent spawn path is unaffected. Stream
  open/write failures log a warning and degrade, never fail the run.

### Mid-run steering (issue #64, multica borrow F3)

- `run_member` attaches an `asyncio.Queue` supplement queue per run
  (detached when the run ends); `TeamRuntime.steer_member(task_id,
  content, origin="operator"|"lead")` queues `[steer] operator note:`
  / `[steer] lead note:` messages. No harness-side cap — the daemon
  owns the 3-per-run cap (agent-studio #748); harness applies whatever
  arrives.
- The member loop drains via `SubagentSession.drain_steers()` at
  AgentGraph node boundaries only — injection points are the drain
  calls, never mid-tool-call. Each drained note records to the run
  stream as a `steer` event, so steering is visible in replay.

### Task wakeup rules (issue #65, multica borrow F4)

- `TeamRuntime.add_wakeup(task_id, rule)` stores `{kind: at|every,
  at/every, on_timeout: wake|end, expires_at}` in
  `task.metadata["wakeups"]`; validation on write; `on_event`
  conditions are phase 2 and rejected in v1; cap 8 armed rules per
  task at the runtime boundary (fired rules free cap space).
- `check_wakeups` fires due rules — `at` exactly once, `every` on its
  interval: `wake` moves a blocked task back to `queued` (the lead's
  pick pool; `queued -> in_progress` transition added), `end` cancels
  with reason `wakeup_timeout`. Expired rules prune lazily on task
  read — no sweeper, no new retention policy. Rules survive
  `save_state`/`load_state`.

Tests: 516 → 555 (39 new). All stdlib, no new dependencies.

## 0.19.0 (2026-11-18)

### Team member context — resident context + SPEC.md injection (issue #61)

- `TeamMember` gains `context: str = ""` — resident per-member knowledge
  passed via `teams.register`; no filesystem semantics (team members are
  in-process `SubagentRunner` runs: no cwd, no AGENTS.md load, so role
  knowledge was previously lost across rounds).
- `TeamRuntime.run_member` now assembles the member prompt before
  dispatch, prepending two sections ahead of `brief.objective`:
  - `[team spec]` — contents of `<state_dir>/SPEC.md`, truncated at 4KB
    (console AGENTS.md cap convention), with the absolute path appended
    so a member whose allowlist includes `file_read` can pull the full
    file; the file is re-read on every dispatch, so SPEC edits apply to
    the next member brief without restart.
  - `[member context]` — the member's resident `context` field,
    truncated at 2KB.
- Graceful degradation: a missing SPEC.md is skipped silently; empty
  sections are omitted (prompt unchanged when both absent); members
  whose `tool_allowlist` lacks `file_read` still get the injected text
  — injection never errors.
- Boundary rulings respected: all team semantics assemble in
  `TeamRuntime.run_member`; `SubagentRunner` is untouched (plain
  subagent spawns unaffected); dispatch stays leader-driven (phase-2
  member self-claim will reuse the same assembly line).

Tests: 503 → 516 (13 new). All stdlib, no new dependencies.

## 0.18.0 (2026-11-18)

Second fusion-mlx research-handoff batch — four features, issues #52-#55.

### Compaction v2 — resolvable archive links (issue #52, ports fusion-mlx M11)

- `fusion_harness.compaction_archive`: append-only JSONL base archive.
  `CompactionArchive.append(messages)` stores evicted tool-result spans
  and returns `archive://span-N` links; compaction clear-stubs cite the
  link so the eviction is resolvable instead of lossy.
- `resolve(link)` replays a link back to its messages;
  `DanglingLinkError` on malformed or unresolvable links (bug, never a
  degraded mode). `rehydrate(messages, archive)` expands links for
  replay; `cite_link` / `extract_links` helpers. Restart-safe via
  reload-on-construct; compaction never mutates written span bytes.

### Handoff verification — permission ledger replay (issue #53, ports M12)

- `PermissionEngine.evaluate()` now records every outcome to an
  in-memory ledger; `export_ledger()` hands the segment to the receiver
  (honest handoffs pay only the replay pass).
- `fusion_harness.handoff_verify`: `verify_handoff(claims, ledger)` —
  a claim passes only on a recorded ALLOW; absent = "not recorded"
  (fabricated), deny/ask = "denied" (unauthorized); both fail loudly,
  never silent-accept. `ledger_for_transport` / `claims_for_transport`
  wrap the payload for `SpawnBrief.metadata`; `verify_spawn_brief`
  treats a missing ledger or claims as a failure.

### Skill library lifecycle — four gates, pruning, routing (issue #54, ports M13)

- `fusion_harness.skills.SkillLibrary` gates every write: generation
  (non-empty provenance), verification (each `SkillCase.must_mention`
  token present in `render_prompt()`), canary (`canary_confirmed`),
  rollback (modification requires current `base_version` — stale base
  rejected). Versioned history per skill; `rollback(name, version)`.
- Usage pruning: `prune(min_uses, window_s)` archives cold skills —
  archived skills stay in the registry but are unselectable; nothing
  hard-deletes. `select(task, top_k)` routes by task-feature token
  overlap over name + description + body.
- `SkillRegistry.replace(skill)` for versioned modification (plain
  `register` still rejects duplicates).

### Telemetry-only annotations (issue #55, ports M14; observation only)

- `trajectory.mark_discardable(record, reason)` annotates spans with
  `payload["discardable"]` — reasons limited to `stale_tool_output` /
  `superseded_turn`; unknown reasons rejected; nothing is actually
  dropped until the fusion-mlx engine API (M14 #1645) exists.
- `PrefixStabilityTracker`: `observe_messages(agent_id, messages)`
  samples the system + first-user prefix; `report()` emits per-agent
  `{stability, turns}` — first turn is baseline, not counted. Harness
  only reports; the warm-list threshold stays with fusion-mlx.

### Process rules (from issue #52 review)

- Prompt-side features are tested through the real object path (no
  string-only assertions on generated prompts).
- Telemetry refactors carry rate/arrival assertions so the metric
  cannot drift silently.

Tests: 453 → 503 (50 new). All stdlib, no new dependencies.

## 0.17.0 (2026-11-18)

### Code-mode tool executor (issue #50, ports fusion-mlx M10)

- `fusion_harness.code_mode`: prompt-side code-mode transform against any
  OpenAI-compatible backend — `render_stubs(tools)` renders the registry
  as a Python stub module with signatures + return-shape hints (pydantic
  inputs normalized via model_dump, M10 #1644 gotcha; parity snapshot test
  vs the fusion-mlx renderer, cross-repo manual trigger not a dependency),
  `parse_code_block` extracts fenced python blocks, `inject_code_mode_
  messages` merges prompt + stubs into the leading system message.
- `CodeExecutor` protocol: harness defines the contract, execution
  backends implement it — harness never runs code itself. Two stub
  renderers coexist by design (harness is stdlib-only, fusion-mlx cannot
  depend on harness).
- `CodeModeTurn`: parse → stub-name mapping (unknown function names
  rejected, mapping strictly bound) → permission admission → execute →
  traceback loop (execution exceptions feed back as the next message for
  self-repair, bounded by max_iterations). Ruled allowlist semantics
  (decision 2026-11-18): the script as a whole passes the permission
  engine in ONE admission pass — every called stub must be allowed,
  unmatched tools evaluate to "ask" in an armed engine and deny, no
  per-stub execution interception; engine errors fail closed. Budget:
  turn refuses when the wired budget_ref reports exceeded (existing
  funnel policy); telemetry events carry the `code` dtype axis.
- `CodeModeMetrics`: dual metric reporting aligned with M15 —
  elicitation (token savings) and expansion (task success rate)
  reported separately, never conflated. 30-turn synthetic trajectory
  replay test lands inside the paper's 70-90% band.
- 33 new tests; suite 420 → 453.

## 0.16.1 (2026-11-17)

### read_segment follow-ups (issue #48)

- UTF-8 chunk-boundary fix: fixed byte ranges no longer misclassify CJK
  segments as binary — an incomplete multibyte tail (up to 3 bytes) is
  held back to the last character boundary and carried by the next
  segment (`next_offset` backs off accordingly); base64 fallback only
  when the trimmed segment is still invalid. Mirrors fusion-console
  e46ca59 (`attachments.go`).
- Budget-exhausted marker: when `SegmentBudget.admit` clamps to 0 bytes
  mid-file, result now carries `budget_exhausted: true` (rendered output
  appends `[budget exhausted for this turn: stop reading this file]`)
  instead of an empty non-eof success that invited same-offset retry
  loops. `budget_exhausted: false` on all normal reads.
- Contract note: `next_offset = end` + `eof: true` is the canonical eof
  shape (fusion-console aligned in e46ca59; fusion-rag #156 to follow).
- 8 new tests; suite 412 → 420.

## 0.16.0 (2026-11-17)

### file.read_segment contract + budget funnel integration (issue #46)

- `fusion_harness.file_segment.read_segment(file_ref, offset, max_bytes,
  policy, budget, turn_id)`: bounded byte-range read primitive (HTTP-Range
  parity — fusion-rag #156 serves the same shape). Hard cap (256 KB
  default) enforced inside the tool layer: oversize requests are CLAMPED,
  never errored whole. Result carries `content`/`total`/`next_offset`/
  `eof`; binary content returns base64 with an `encoding` flag. Missing
  file raises (fail-visible).
- Citable header: every result starts with
  `[segment file=... bytes=START-END/TOTAL]` — compaction eviction embeds
  the header in the cleared stub and `RetentionList` files segment headers
  under constraints, so summaries cite what was already read.
- `SegmentPolicy(max_bytes_cap, per_turn_bytes)` + `SegmentBudget`:
  per-(turn, file_ref) byte ledger — bounds total attachment consumption
  per turn independent of call count; exhaustion clamps to 0 bytes (eof
  stays False), never errors. `render_segment_result` produces the
  funnel-ready string that flows through `ToolFunnel.run_tool` like any
  tool output — no special casing downstream.
- 27 new tests (incl. compaction preservation); suite 385 → 412.

## 0.15.0 (2026-11-17)

### HumanGate: async human-approval hook + sink (issue #44)

- `fusion_harness.human_gate.HumanGate(state_dir, bus)`: structured question
  queue for long-running sessions — `ask(session_id, question, payload,
  reply_schema, policy)` emits to a persistent on-disk queue
  (`human_gate_queue.json`, atomic tmp+replace writes) plus a
  `human_gate.question` lifecycle event; `reply(question_id, answer)`
  validates against the reply schema (deliberate JSON-schema subset:
  required + per-key type names, bool-never-int-field), marks answered,
  wakes in-process waiters. Invalid reply rejected loudly, question stays
  pending. `cancel` wakes waiters with None.
- Policies: `block` = `await wait(question_id, timeout)` park point,
  resolves with reply; already-answered (including answered before a
  restart) resolves immediately. `continue` = session proceeds; reply
  consumed later via `take_reply` (consumed-once semantics).
- Restart survival: pending questions AND unconsumed replies both reload
  on construction; corrupt entries skipped with warning, corrupt file
  starts empty. Session routing via `pending(session_id)`.
- Additive sink kind beside existing permission sinks — no engine-loop
  changes, zero model calls. 30 new tests; suite 385.

## 0.14.1 (2026-11-16)

### CI efficiency retrofit (issue #41, fusion#2 checklist)

- Workflows: concurrency cancel groups by ref on ci/docs (release grouped,
  never cancelled mid-publish); pip dependency cache keyed on pyproject.toml
  for ci matrix + release jobs; sharding and nightly assessed as not needed
  (full matrix 15-47s). Workflow-only change, zero code delta.

## 0.14.0 (2026-11-16)

### Chat standardization C1 + lenient coercion (issues #38 #39)

- `team_runtime` (#38, C1): `TeamMessage` extended — `msg_id` generated at
  send ("msg_"+hex8, stored in inbox record; `send` now returns
  `{"delivered": bool, "msg_id": str}`), `reply_to` (format-validated msg_
  prefix, existence unchecked), `mentions` (declared members only, unknown
  = TeamStateError at send/broadcast time; broadcast rejects up front),
  `attachments` (artifact refs, shape-validated only), `audio_ref` +
  `transcript` (voice = text message with fields; MSG_TYPES unchanged).
  Pre-extension inbox records load with defaults. Additive; bare-bool
  callers adapt one line.
- `dag_node.coerce_params` (#39): opt-in `strict=False` lenient mode with
  cowork parity for LLM-generated params — per-field coercion, unconvertible
  values pass through with warning, "3.7" integer truncates, "a,b,c" array
  CSV-splits. Strict default unchanged.
- 8 new tests; suite 355. contract.md §23 + §25 updated.

## 0.13.0 (2026-11-14)

### Kernel convergence K1-K3 (issues #34 #35 #36, roadmap kernel-abstract-roadmap-2026-11-14)

- `fusion_harness.scoped_permissions` (#34, K1): scoped permission engine
  ported from cowork's 5-level permission_manager — deny-rules-first,
  file:/command:/url: scope prefixes (kernel adds path-boundary check and
  subject/expires_at/multi-scope), high-risk guard delegation
  (register_guard, engine owns no guard logic, fail-closed deny + queue),
  durable approval queue over CheckpointStore. Existing
  fusion_harness.permissions API untouched.
- `fusion_harness.dag_node` (#35, K2): BaseNode / NodeRegistry (duplicate
  rejection) / NodeContext / strict param coercion — kernel raises
  ParamCoercionError naming field+expected+received where cowork silently
  passes values through (intentional, recorded in parity table).
- `fusion_harness.checkpoint_binding` (#35, K2): canonical
  bind_node_state(store, outbox, run_id) — per-node CheckpointStore state +
  OutboxStore receipt idempotency (mark_emitted runs once; crash-recovery
  replay never re-executes committed effects).
- `fusion_harness.artifact_channel.ArtifactTools` (#36, K3): capture/read/
  list/diff over an existing ArtifactStore; refs-only interchange preserved.
- `fusion_harness.trajectory` (#36, K3): one normative JSONL record schema
  (DAG node_id and team member union; model_call_meta carries model name +
  token counts only — prompt/completion content rejected at construction);
  reader normalizes legacy cowork and studio shapes for gradual migration.
- Zero-model CI check extended to all new modules. 78 new tests; suite 347.
  contract.md §24-§26.

## 0.12.1 (2026-11-14)

### Depth enforcement in run_member (audit follow-up to #31)

- `TeamRuntime.run_member` now takes `depth` (caller's global orchestration
  depth, default 1) and raises typed `TeamStateError` when
  `depth + TeamDefinition.max_depth` exceeds 3 — max_depth was validated
  at definition time but not enforced at spawn time; closes the hole T3
  member-internal re-spawn would hit. 22 team tests (suite 269).

## 0.12.0 (2026-11-14)

### Team entity (issue #31, T1)

- `fusion_harness.team`: `TeamMember` + `TeamDefinition` + typed
  `TeamDefError` (duplicate names, empty namespace_key, depth overflow,
  member budget over lead budget). Pure validators, fail-visible.
- `fusion_harness.team_runtime`: `TeamRuntime` — lifecycle state machine
  (DEFINED -> ACTIVE to PAUSED and back -> DISBANDED, typed errors), task board
  (whitelisted transitions, blocked requires blocked_by, CAS-atomic
  claims), `run_member` (SpawnBrief enforcement + constraint stacking
  that can only tighten + summary lands on the board), JSON mailbox
  (delivered only on successful file write, type enum validated,
  broadcast one-per-member, never auto-injected), `save_state`/
  `load_state` round-trip. All events on LifecycleBus.
- Zero model calls in any TeamRuntime API (CI-greppable test).
- 21 new tests; suite 268. contract.md §23.

## 0.11.0 (2026-11-14)

### Collaboration discipline layer (issues #26-#29, insight 2026-11-14)

- `fusion_harness.spawn_protocol` (#26): SpawnBrief contract (7 required
  fields, typed SpawnBriefError on incomplete — vague briefs are bugs) +
  deterministic EffortTierConfig.resolve (1 agent <=3 calls; 2-4 subagents
  3-10; 10+ split >10). Pure functions, no model call.
- `fusion_harness.artifact_channel` (#27): ArtifactStore (full worker
  outputs to per-run dirs) + SummaryInjector (compressed summaries +
  refs at the FRONT of coordinator context, idempotent; Lost in the
  Middle placement). Deterministic fallback summarizer, injectable.
- `fusion_harness.playbook` (#28): append-only op log (add/update/delete
  with mandatory provenance episodes), tombstoned deletes, render() fold,
  trace() provenance. Anti brevity-bias / context-collapse (ACE).
- `fusion_harness.cache_discipline` (#29): assert_stable_prefix (byte-
  identical prefix check), check_append_only (mutation only at compaction
  boundaries), mask_tools + ToolAvailabilityState (mask, never remove —
  KV prefix stays valid across availability changes).
- 17 new tests; suite 247. contract.md §19-§22.

## 0.10.1 (2026-11-13)

### Security intel loop (issue #24, fusion-security contract v1)

- `fusion_harness.security_intel.RiskIntelClient`: calls fusion-security
  `POST /api/v1/agent/risk-score` (wire contract:
  `schemas/agent-risk-score.v1.json`, fusion-security PR #89) and pushes
  harness allow/deny decisions back to `/api/v1/agent/decisions` —
  closed feedback loop into the security audit stream.
- Opt-in `ToolFunnel(security_intel=...)`: unwired or unreachable endpoint
  = exact previous behavior (logged no-op, offline-first). Hints advisory
  by default (`security_intel` lifecycle event); `enforce_hints=True`
  turns an intel deny hint into a funnel denial. Harness stays the
  deny-first executor — permission decisions never overridden by intel.
- Exports: `RiskIntel`, `RiskIntelClient`. contract.md §18.
- 7 new tests (contract shape, advisory default, enforced deny, decision
  push on deny paths, offline no-op, unwired no-change). Suite: 230.

## 0.10.0 (2026-11-12)

### MCP HTTP transport (P1-1 completion)

- `fusion_harness.mcp.MCPStreamHttpClient`: Streamable-HTTP transport
  (JSON-RPC POST, JSON or SSE response, `Mcp-Session-Id` continuity) —
  same surface as the stdio client; pure stdlib (urllib off-thread).
- `MCPToolSource` unchanged — both transports feed the funnel identically.

### Orchestration primitives (kernel half of the ladder)

- `fusion_harness.orchestrator`: `BackgroundPool` (spawn/collect) +
  `ParallelTeam` (concurrent fan-out, sibling failures never cancel).
  Thin concurrency shapes over `SubagentRunner` — summary-only return,
  mode narrowing, budget isolation inherited. Workflow/DAG stays in
  fusion-cowork (contract.md §17).
- Exports: `BackgroundPool`, `ParallelTeam`.

### Consumer requirements filed

- agent-studio#675 (kernel wiring + sandbox runner + tokenizer),
  cowork#470 (DAG checkpoint binding + workflow ladder),
  mlx#1559 (CacheAdvisor + tokenizer + nightly bench),
  memory#42 (MemorySink/Source adapters).

## 0.9.1 (2026-11-12)

### Acceptance verification (evolution plan audit)

- `SubagentRunner(ledger_factory=...)`: per-subagent `TokenLedger` — token
  accounting goes through the ledger balance (single ownership, spec §3.4),
  caps enforced on recomputed balance, never a drift-prone field.
- `SubagentRunner.child_engine(parent_engine, spec)`: child
  `PermissionEngine` built from the parent's full rule snapshot + node-layer
  allow rules derived from the spec allowlist; deny anywhere still wins
  (deny returns on first match, ask outranks pending allow); child mode
  narrows, never widens.
- `PermissionEngine.rules_snapshot()`: flat rule list across global/graph/node
  layers, for building child engines without widening.
- New `tests/test_acceptance_verification.py` (14 tests): MCP deny-path
  through `ToolFunnel` (denied `tools/call` never reaches the server),
  compaction thresholds switching on counter identity, survival contract
  across warning/error/manual + reactive strip + MLX-gateway path, subagent
  ledger budget + child engine narrowing, SIGKILL-mid-session checkpoint
  reopen, receipt-cache replay no-op.
- `docs/plan-verification.md`: full acceptance-criteria -> test matrix for
  evolution-plan-2026-11; kernel scope 100% covered, consumer-tracked items
  listed explicitly.
- `docs/contract.md` §13: risk band -> execution tier mapping table.

## 0.9.0 (2026-11-12)

### Phase 3 surpass bets (evolution plan P3-1..P3-3)

- `fusion_harness.sandbox` (P3-3, G8): `ExecutionTier` (process/sandbox/
  blocked) + `SandboxPolicy` — destructive-band tools route to a
  consumer-provided runner; sandbox tier with no runner wired is denied
  (fail-closed); `ToolFunnel(sandbox_policy=...)` routes between admission
  and execution. Enforcement mechanism, not a second policy engine.
- Cache advisor (P3-1, G7): `CompactionConfig.cache_advisor` — engine-side
  advice on how many rounds to keep so compaction lands at stable prefix
  boundaries; advisor can only raise the keep window, never shrink it;
  advisor failures fall back to config.
- `fusion_harness.memory_link` (P3-2, G9): `MemorySink`/`MemorySource`
  protocols + `MemoryBridge` — compaction summaries persist to a sink,
  recall is just-in-time injection (bounded, after system prefix), recall
  failure is a no-op (memory is an upgrade, never a dependency).

## 0.8.0 (2026-11-12)

### Phase 2 structural upgrade (evolution plan P2-1..P2-4)

- `fusion_harness.checkpoint` (P2-2, G4): `CheckpointStore` (sqlite WAL +
  `synchronous=FULL`) — durable `CheckpointState` (messages, budget, mode,
  outbox offset), save/latest/load/list/prune; resume composes with outbox
  receipt idempotency so committed tool effects never re-execute.
- `fusion_harness.subagent` (P2-1, G3): `SubagentRunner` + `SubagentSpec` +
  `SubagentSession` — one subagent primitive (not a fleet framework):
  isolated child surface, tool allowlist intersected with parent surface,
  permission mode can only tighten (bypass > approved > default > plan),
  per-subagent budget/turn caps flag `capped`, summary-only return via
  `SUBAGENT_*` lifecycle events.
- `fusion_harness.skills` (P2-3, G2): SKILL.md-style `parse_skill` +
  `SkillRegistry` + deterministic `derive_permissions()` (2611.08905) —
  node-layer rules, deny-wins conflicts, unknown permission keys and
  missing names rejected at load (fail-closed).
- `bench/` (P2-4, G10): deterministic "future questions" compaction eval
  (2610.27301) with `bench/baseline.json` regression gate wired into CI.

## 0.7.0 (2026-11-12)

### Phase 1 convergence surface (evolution plan P1-1..P1-4)

Gap-fill per `docs/evolution-plan-2026-11.md` — MCP transport, real token
counting seam, compaction survival contract, tool-result clearing. Pure
stdlib; additive; no behavior change without config.

- `fusion_harness.tokens` (P1-2): `TokenCounter` protocol +
  `HeuristicTokenCounter` (legacy chars/4, default) +
  `CJKAwareHeuristicTokenCounter` (CJK ~= 1 token/char); `count_messages`
  helper; `CompactionConfig.token_counter` injects a model-accurate
  counter into the compactor.
- Tool-result clearing (P1-4): deterministic compaction stage collapses
  tool results older than `keep_recent_rounds` to identity stubs
  (`[tool result cleared: id=... bytes=...]`) before any summarization —
  Anthropic's safest-first principle.
- Survival contract (P1-3): pinned segments survive every compaction path
  — system messages validated verbatim post-compaction (degradation is
  logged as error and re-inserted), task goal re-pinned as
  `[Pinned task goal]` when compacted away; `Compactor.survival_manifest()`
  introspection.
- `fusion_harness.mcp` (P1-1): stdlib MCP stdio client
  (`MCPStdioClient` initialize/tools-list/tools-call over JSON-RPC line
  wire) + `MCPToolSource` exposing tools as `mcp__<server>__<tool>` for
  the funnel's existing permission engine (deny-first unchanged; transport
  carries no privilege).

## 0.6.0 (2026-11-10)

### Provider seam: adapter contract + ACP-compatible wire (#15)

Pattern adoption from t3code (provider-core / provider-acp server seam) —
re-implemented stdlib-only, no upstream code vendored. One adapter per agent
runtime; the host is runtime-agnostic; the seam is a transport, not a second
policy engine (funnels / permission engine / compaction apply upstream,
unchanged).

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.provider.events`: UI-agnostic `SessionEvent` model +
  `EventBus` fan-out (stable wire event-type constants).
- `fusion_harness.provider.adapter`: `ProviderAdapter` abstract contract
  (start/stop/new_session/prompt/respond_permission/close_session) and
  `AdapterHost` — registry, id allocation, and the HiTL permission seam
  (`request_permission` is fail-closed: no handler or handler error = deny).
- `fusion_harness.provider.wire`: ACP-compatible JSON-RPC 2.0 codec,
  line-delimited (initialize / session-new / session-prompt /
  session-update notifications / request-permission round-trip).
- `fusion_harness.provider.session`: `SubprocessSessionRuntime` — spawns the
  adapter driver subprocess, lifecycle state machine
  (new/ready/running/awaiting_permission/closed), streaming notifications to
  events, driver permission requests routed through `AdapterHost`.
- Top-level exports: `AdapterHost`, `ProviderAdapter`, `SubprocessSessionRuntime`,
  `RuntimeNotReady`, `ProviderSessionEvent`, `ProviderSessionState`
  (aliased; plain `SessionEvent`/`SessionState` remain the session-log types).

Non-goals (unchanged from the issue): no t3code provider fleet, no source-control
integration, no hosted relay, 100% offline.

## 0.5.0 (2026-11-08)

### Cross-cutting concern ownership: deadline / retry / idempotency / metering (#13)

New public API (semver-minor: additive, no breaking changes), spec
`verification-infra-spec-1108.md` §3.4 — one ownership point per concern,
consumers opt in:

- `Deadline` / `DeadlineExceeded`: caller declares the deadline once;
  `child()` budgets only shrink; `check()` fails honestly at expiry
  (cow#378 dual-timeout fix).
- `RetryPolicy` + `run_with_retry()`: single retry mediation point —
  exponential backoff capped at `max_backoff`, clamped to the deadline;
  `coalesce_retry()` collapses stacked layer policies with a loud warning
  (as#615 / as#649 outage-window fix). `TransientError` is the marker base
  for retryable failures.
- `idempotency_key()` + `ReceiptCache`: canonical `(scope, node, action)`
  keys and a standalone receipt table; `OutboxStore` command receipts (#11)
  now delegate to `ReceiptCache` — single idempotency implementation.
- `TokenLedger`: append-only authoritative usage ledger; balance is always
  a recomputed sum, JSONL-persisted and rebuildable.
- `docs/ownership-map.md`: one-page ownership map with per-repo removal
  list for private timeout/retry/idempotency code.

## 0.4.0 (2026-11-08)

### Transactional outbox + idempotent command receipts + startup reconcile (#11)

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.outbox` (#11): stdlib-only SQLite (WAL, `synchronous=FULL`)
  transactional outbox — `OutboxStore.commit_step()` persists run events and
  side-effect enqueue in one transaction; `claim_next()`/`settle()` give
  serial at-least-once delivery with a lease and **persisted** `available_at`
  backoff; `record_command()`/`execute_command()` dedupe replays by
  `command_id` receipt (duplicate-issue/PR guard, cowork#381);
  `reconcile(orphaned_run_ids)` requeues expired leases and retires
  orphaned-run effects at boot (t3code `ProviderRuntimeRecoveryService`
  pattern). Design note: `docs/adr-0002-transactional-outbox.md`.


## 0.3.1 (2026-11-07)

### Docs-system v1.0 adoption (P1 pilot)

- Adopted the fusion docs-system v1.0 contract (grid position: platform x
  developer): Tier 0 (`docs/product-page.md`, `docs/get-started.md`),
  Tier 1 (`docs/api-reference.md`, `docs/config-reference.md`,
  `docs/architecture.md`, recipes), frontmatter contract headers on
  README/CHANGELOG/contract/ADR.
- `ci/validate_docs.py`: stdlib-only static validator (frontmatter
  completeness, doc-set inventory, source_of_truth paths, redline scan,
  line budgets) + `docs.yml` CI workflow gating markdown PRs.
- Validator messages/comments are English-only; English-only source
  constraint applied repo-wide (PR #10).

## 0.3.0 (2026-10-28)

### Segmented tool pipeline, durable session log, engine-level confirmation tiers (#4/#5/#9)

New public API (semver-minor: additive, no breaking changes):

- `fusion_harness.session_log` (#4): durable append-only `SessionEventLog`
  (schema-versioned frozen `SessionEvent` records, monotonic seq, JSONL +
  fsync persistence, `SessionEventLog.load()` restart reconstruction,
  `replay()` reducer with explicit `incomplete_tool_calls` for crash
  mid-tool-call). Live `LifecycleEvent`s are structurally rejected
  (`TypeError`) — domain separation, not convention.
- `fusion_harness.pipeline` (#5): five-stage `ToolPipeline` — pre-execute
  waterfall → identity-protected `MonotonicGuard`s (immutable tuple, deny is
  final) → one-shot fail-closed approval → around-dispatch (timeout/retry) →
  post-execute waterfall, ending in a frozen `ToolResult`.
- `PermissionEngine` confirmation tiers (#9): `ConfirmationPolicy`
  (always/never/risk) + deterministic `score_tool_risk()` bands evaluated
  inside the engine; `evaluate(can_confirm=...)` fail-closed on
  non-interactive paths; `FunnelOutcome.confirmation` carries the decision.
- `LLMFunnel.pre_llm` compaction success tracking (#7): `compact_after`
  payload carries `success` + error; failed compaction passes original
  messages through unchanged.

0.3.0 consumer-integration additions:

- `ToolPipeline.run(approver=...)`: per-call approval responder — shared
  pipeline instances stay safe under concurrent admits.
- `ToolPipeline.run(admission_only=True)`: stages 1-3 only, dispatch
  deferred (consumers that execute tool bodies elsewhere).
- `permission_engine.evaluate()` and `approver.request()` may be async
  (awaitables are awaited in place) — async funnel-backed policy adapters
  and interactive approval bridges work without wrappers.

Consumer adoption: fusion-agent-studio routes every tool admission through
`ToolPipeline` (single admission path, no side doors) and records every
session into a durable `SessionEventLog` — issues #4/#5 closed end-to-end.

## 0.2.2 (2026-10-27)

Features of #4/#5/#9 as initially released to PyPI (superseded by 0.3.0's
consumer-integration additions above).

## 0.2.1 (2026-10-26)

### PyPI release pipeline (#3)

- Published to public PyPI as `fusion-harness`. Tag pushes (`v*`) now run
  `.github/workflows/release.yml`: test → build (`python -m build` +
  `twine check`) → publish via PyPI trusted publishing (OIDC, no token).
- Consumers no longer need `pip install git+...` preinstall steps —
  fusion-agent-studio and fusion-cowork CI now install from PyPI.
- Added `.github/workflows/ci.yml`: ruff + pytest on Python 3.11/3.12/3.13
  for every push to master and every PR.

## 0.2.0 (2026-10-25)

### Breaking: permission enforcement fails closed by default (#1)

`ToolFunnel` previously passed tool calls through when no `PermissionEngine` was
wired (opt-in enforcement via `FUSION_FUNNEL_REQUIRE_PERMISSIONS=1`). The
default is now **deny** — an absent engine is treated as a wiring bug.

Migration for operators:

- Headless/CI deployments that relied on passthrough must opt out explicitly:
  `FUSION_FUNNEL_PERMISSIONS=off` (or the legacy name
  `FUSION_FUNNEL_REQUIRE_PERMISSIONS=0`).
- Every opt-out skip logs `permission enforcement DISABLED via <var> — headless
  deployment?` so the state is always visible in logs.
- Deny-first rules, the 12 dangerous-pattern matchers, and plan-mode gating are
  unchanged — only the default changed.

### Kernel consolidation (#2)

- `fusion-harness` is now a hard dependency of fusion-agent-studio and
  fusion-cowork; their in-tree `_local` fallback copies and `KERNEL_SOURCE`
  shims are deleted. Import failure is a loud `ImportError`.
- `docs/contract.md`: the kernel behavior contract (funnel order, permission
  modes, hook fail-closed rules, compaction levels, env vars) — the TypeScript
  implementation in fusion-code conforms to this contract via its own parity
  tests rather than sharing runtime code.
- Test suite grew from 5 smoke tests to 59: per-module unit coverage
  (funnels/permissions/hooks/compactor) including the fail-closed acceptance
  criteria from #1.
- Fixed: callback-hook exceptions on fail-closed events
  (`PRE_TOOL_USE`/`POST_TOOL_USE`/`TOOL_DENY`) now block instead of being
  swallowed (audit P-3 completion).

## 0.1.0 (2026-10-25)

Initial extraction from fusion-agent-studio N0 kernel (#357).
