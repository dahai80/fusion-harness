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
