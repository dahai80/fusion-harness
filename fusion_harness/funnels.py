# Canonical kernel module lives here; agent_runtime imports via shim (#357).
"""N0 harness funnels — structural control points for tool/LLM/lifecycle.

Three architecture funnels (PRD 1016):
- LifecycleBus: append-only session event source (DSH-informed). All state
  changes flow through it; replayable; three event domains (session/agent/
  capability).
- ToolFunnel: THE single tool admission path — lifecycle -> hooks ->
  permission (deny-first) -> budget -> execute. Node code has no bypass
  route: they call funnel.run_tool or funnel.admit; the scattered
  safety_gateway.evaluate_action call sites in the node executors are
  collapsed into this chain.
- LLMFunnel: single LLM admission path — lifecycle -> hooks -> compaction ->
  metering.

Fail-closed: a failing security-gate hook or permission check blocks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fusion_harness.permissions import (
    PermissionDecision,
    PermissionEngine,
    score_tool_risk,
)
from fusion_harness.sandbox import ExecutionTier, SandboxPolicy

logger = logging.getLogger(__name__)


_PERMISSIONS_OFF_VALUES = ("0", "false", "no", "off", "disabled")


def _permissions_disabled_var() -> str | None:
    # Issue #1: enforcement is fail-closed by default. Opt-out (headless/CI
    # deployments) must be explicit — and loud, never silent.
    for var in ("FUSION_FUNNEL_PERMISSIONS", "FUSION_FUNNEL_REQUIRE_PERMISSIONS"):
        val = os.environ.get(var, "").strip().lower()
        if val in _PERMISSIONS_OFF_VALUES:
            return var
    return None


class EventDomain(str, Enum):
    SESSION = "session"
    AGENT = "agent"
    CAPABILITY = "capability"


@dataclass
class LifecycleEvent:
    seq: int
    ts: float
    domain: str
    event: str = ""
    payload: dict = field(default_factory=dict)
    session_id: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "domain": self.domain if isinstance(self.domain, str) else self.domain.value,
            "event": self.event,
            "payload": self.payload,
            "session_id": self.session_id,
        }


class LifecycleBus:
    """Append-only lifecycle event source (three domains, replayable).

    In-memory bounded ring per session plus optional JSONL persistence.
    Subscribers get called synchronously on emit (hook consumption point);
    subscriber failures never break the emit (logged, fail-open for notify
    only — security gates are enforced by ToolFunnel, not here).
    """

    def __init__(self, maxlen: int = 10000, jsonl_dir: str | None = None):
        self._lock = threading.Lock()
        self._seq = 0
        self._events: dict[str, deque] = {}
        self._maxlen = maxlen
        self._jsonl_dir = jsonl_dir
        self._subscribers: list = []

    def subscribe(self, fn) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass

    def emit(
        self,
        domain: EventDomain | str,
        event: str,
        payload: dict | None = None,
        session_id: str = "",
    ) -> LifecycleEvent:
        with self._lock:
            self._seq += 1
            ev = LifecycleEvent(
                seq=self._seq,
                ts=time.time(),
                domain=domain.value if isinstance(domain, EventDomain) else str(domain),
                event=event,
                payload=payload or {},
                session_id=session_id,
            )
            dq = self._events.setdefault(ev.session_id, deque(maxlen=self._maxlen))
            dq.append(ev)
        if self._jsonl_dir:
            self._append_jsonl(ev)
        for fn in list(self._subscribers):
            try:
                fn(ev)
            except Exception as e:
                logger.warning("lifecycle subscriber error: %s", e)
        return ev

    def replay(
        self, session_id: str | None = None, domain: str | None = None, limit: int = 0
    ) -> list[LifecycleEvent]:
        with self._lock:
            if session_id:
                events = list(self._events.get(session_id, deque()))
            else:
                events = [e for dq in self._events.values() for e in dq]
        if domain:
            events = [e for e in events if e.domain == domain]
        if limit:
            events = events[-limit:]
        return events

    def session_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._events.get(session_id, ()))

    def _append_jsonl(self, ev: LifecycleEvent) -> None:
        try:
            from pathlib import Path

            d = Path(self._jsonl_dir)
            d.mkdir(parents=True, exist_ok=True)
            name = ev.session_id or "global"
            path = d / f"{name}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as e:
            logger.warning("lifecycle jsonl append failed: %s", e)


@dataclass
class FunnelOutcome:
    """Result of the tool admission chain (decision log preserved)."""

    allowed: bool
    blocked_by: str = ""
    reason: str = ""
    args: dict = field(default_factory=dict)
    needs_approval: bool = False
    safety_result: Any = None
    decision_log: list = field(default_factory=list)
    # #9: engine-level confirmation decision (tier/risk/confirmation), so
    # consumers don't re-derive a local policy layer. None = no policy wired.
    confirmation: dict | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "needs_approval": self.needs_approval,
            "decision_log": self.decision_log,
            "confirmation": self.confirmation,
        }


class ToolFunnel:
    """Single tool admission + execution path.

    Chain (order IS the contract, deny-first at permission stage):
        lifecycle(tool_before) -> hooks(PRE_TOOL_USE) -> permission(deny-first)
        -> [needs_approval -> runtime handles future] -> budget -> execute
        -> lifecycle(tool_after) -> hooks(POST_TOOL_USE)
    """

    def __init__(
        self,
        hooks=None,
        permissions: PermissionEngine | None = None,
        lifecycle_bus: LifecycleBus | None = None,
        budget_ref=None,
        audit_logger=None,
        readonly_tools=None,
        safety_gateway=None,
        sandbox_policy: SandboxPolicy | None = None,
        security_intel=None,
    ):
        self.hooks = hooks
        self.permissions = permissions
        self.lifecycle = lifecycle_bus or LifecycleBus()
        self.budget_ref = budget_ref
        self.audit_logger = audit_logger
        # readonly_tools may be a set (static) or a zero-arg callable returning
        # a set (dynamic — registry can gain tools after funnel construction).
        self.readonly_tools = readonly_tools or set()
        self.safety_gateway = safety_gateway
        self.sandbox_policy = sandbox_policy
        # Optional fusion-security intel client (issue #24): advisory hints,
        # graceful no-op offline, decisions pushed back to the audit stream.
        self.security_intel = security_intel
        self._executions = 0

    def set_budget_ref(self, budget_ref) -> None:
        self.budget_ref = budget_ref

    async def admit(
        self,
        ctx=None,
        tool_name: str = "",
        args: dict | None = None,
        category: str = "tool_call",
        node_label: str = "",
        content_repr: str = "",
        can_confirm: bool = True,
    ) -> FunnelOutcome:
        """Run the admission chain. Does NOT execute the tool."""
        args = args or {}
        log: list = []

        def _mark(stage: str, verdict: str, reason: str = "") -> None:
            log.append({"stage": stage, "verdict": verdict, "reason": reason})

        self._emit(
            "capability", "tool_before", {"tool": tool_name, "args": args, "node": node_label}, ctx
        )

        # Stage 1: hooks (PRE_TOOL_USE) — rewrite or block.
        hook_out = None
        if self.hooks is not None:
            try:
                hook_out = await self.hooks.fire(
                    "PRE_TOOL_USE", {"tool_name": tool_name, "args": args}, tool_name=tool_name
                )
            except Exception as e:
                logger.warning("funnel hook fire error tool=%s err=%s", tool_name, e)
                hook_out = None
            if hook_out is not None:
                if getattr(hook_out, "decision", "approve") == "block":
                    _mark("hook", "deny", getattr(hook_out, "reason", "") or "pre_tool_use block")
                    return self._deny(ctx, tool_name, args, "hook", log, hook_out.reason)
                if getattr(hook_out, "updated_input", ""):
                    try:
                        args = (
                            json.loads(hook_out.updated_input)
                            if isinstance(hook_out.updated_input, str)
                            else dict(hook_out.updated_input)
                        )
                        _mark("hook", "rewrite", "updated_input applied")
                    except (TypeError, ValueError):
                        _mark("hook", "passthrough", "updated_input not JSON args, ignored")

        # Stage 2: permission engine (deny-first). plan_mode gate lives in the
        # engine's plan gate (ctx.plan_mode + not readonly -> deny).
        readonly = self._readonly_names()
        perm_decision: PermissionDecision | None = None
        disabled_var = _permissions_disabled_var()
        if disabled_var is not None:
            # Issue #1: explicit opt-out only — and every skipped call logs a
            # prominent warning so no deployment is silently unarmed.
            logger.warning(
                "permission enforcement DISABLED via %s — headless deployment? tool=%s",
                disabled_var,
                tool_name,
            )
            _mark("permission", "skip", f"enforcement disabled via {disabled_var}")
        elif self.permissions is None:
            # Issue #1: an absent engine is a wiring bug, never a silent
            # allow. Fail-closed by default (N8 #391 precedent).
            _mark("permission", "deny", "permission engine absent (fail-closed)")
            logger.error(
                "tool funnel has no permission engine tool=%s fail_closed=true",
                tool_name,
            )
            return self._deny(
                ctx,
                tool_name,
                args,
                "permission",
                log,
                "permission engine absent (fail-closed)",
            )
        else:
            try:
                perm_decision = self.permissions.evaluate(
                    tool_name,
                    args,
                    ctx=ctx,
                    readonly_hint=tool_name in readonly,
                    can_confirm=can_confirm,
                )
            except Exception as e:
                logger.warning("permission evaluate error tool=%s err=%s", tool_name, e)
                perm_decision = PermissionDecision(
                    decision="ask", rule_source="error_fallback", reason=f"permission error: {e}"
                )
            # #9: carry the engine's confirmation decision on every outcome.
            conf = None
            if getattr(perm_decision, "tier", ""):
                conf = {
                    "tier": perm_decision.tier,
                    "risk": perm_decision.risk,
                    "confirmation": perm_decision.confirmation,
                }
            if perm_decision.decision == "deny":
                _mark("permission", "deny", perm_decision.reason)
                denied = self._deny(ctx, tool_name, args, "permission", log, perm_decision.reason)
                denied.confirmation = conf
                return denied
            _mark("permission", perm_decision.decision, perm_decision.reason)
            if perm_decision.decision == "ask":
                return FunnelOutcome(
                    allowed=False,
                    blocked_by="permission",
                    reason=perm_decision.reason,
                    args=args,
                    needs_approval=True,
                    decision_log=log,
                    confirmation=conf,
                )

        # Stage 2b: SafetyGateway evaluation (legacy backend evaluator — the
        # funnel is the only entry; the gateway remains the approval-queue
        # implementation, R1 归一 = entry unification). Block -> deny;
        # requires_approval -> outcome carries the safety_result so the
        # runtime approval flow (futures/timeout/disposition) stays intact.
        if self.safety_gateway is not None:
            try:
                safety_result = self.safety_gateway.evaluate_action(
                    category=category,
                    content=content_repr or f"{tool_name}({args})",
                    context=f"tool={tool_name} node={node_label} path=funnel",
                )
            except Exception as e:
                logger.warning("safety evaluate error tool=%s err=%s", tool_name, e)
                safety_result = None
            if safety_result is not None:
                if safety_result.action.value == "block" and not safety_result.requires_approval:
                    _mark("safety", "deny", safety_result.reason)
                    return self._deny(ctx, tool_name, args, "safety", log, safety_result.reason)
                if safety_result.requires_approval:
                    _mark("safety", "ask", safety_result.reason)
                    return FunnelOutcome(
                        allowed=False,
                        blocked_by="safety",
                        reason=safety_result.reason,
                        args=args,
                        needs_approval=True,
                        decision_log=log,
                        safety_result=safety_result,
                    )

        # Stage 3: token budget.
        budget = None
        if self.budget_ref is not None:
            budget = self.budget_ref
        if budget is not None and _budget_exceeded(budget, getattr(ctx, "graph_id", "") or ""):
            _mark("budget", "deny", "token budget exceeded")
            return self._deny(ctx, tool_name, args, "budget", log, "token budget exceeded")

        _mark("admit", "allow", "all gates passed")
        out = FunnelOutcome(allowed=True, args=args, decision_log=log)
        if perm_decision is not None and getattr(perm_decision, "tier", ""):
            out.confirmation = {
                "tier": perm_decision.tier,
                "risk": perm_decision.risk,
                "confirmation": perm_decision.confirmation,
            }
        if perm_decision is not None:
            out.decision_log.append(
                {
                    "stage": "permission",
                    "verdict": "allow",
                    "rule_source": perm_decision.rule_source,
                }
            )
        return out

    def _readonly_names(self) -> set:
        # Support both static sets and a callable provider (registry may gain
        # tools after funnel construction — dynamic/plugin tools).
        ro = self.readonly_tools
        if callable(ro):
            try:
                return ro() or set()
            except Exception as e:
                logger.warning("readonly_tools provider failed: %s", e)
                return set()
        return ro or set()

    def _deny(
        self, ctx, tool_name: str, args: dict, gate: str, log: list, reason: str
    ) -> FunnelOutcome:
        out = FunnelOutcome(
            allowed=False, blocked_by=gate, reason=reason, args=args, decision_log=log
        )
        self._emit(
            "capability",
            "tool_deny",
            {"tool": tool_name, "gate": gate, "reason": reason, "args": args},
            ctx,
        )
        if self.audit_logger is not None:
            try:
                self.audit_logger.log(
                    actor="runtime",
                    action=f"funnel.deny.{gate}",
                    resource=tool_name,
                    details={"reason": reason, "args": args},
                )
            except Exception as e:
                logger.warning("funnel audit log failed: %s", e)
        return out

    def _emit(self, domain, event: str, payload: dict, ctx) -> None:
        try:
            self.lifecycle.emit(
                domain, event, payload, session_id=getattr(ctx, "session_id", "") or ""
            )
        except Exception as e:
            logger.warning("lifecycle emit failed event=%s err=%s", event, e)

    async def run_tool(
        self, ctx, tool_name: str, args: dict, executor, node_label: str = ""
    ) -> tuple[str, bool]:
        """Admit then execute; returns (result_str, is_error).

        `executor` is a callable (tool, validated_args) -> awaitable str/ToolResult —
        supplied by the runtime so validation/config-merge stay where they are.
        """
        outcome = await self.admit(ctx=ctx, tool_name=tool_name, args=args, node_label=node_label)
        if outcome.needs_approval:
            return (
                f"Error: tool '{tool_name}' requires approval and no approval handler "
                f"is wired to this funnel; call from the runtime approval path",
                True,
            )
        if not outcome.allowed:
            blocked = f"Blocked by {outcome.blocked_by}: {outcome.reason or 'denied'}"
            logger.info(
                "funnel blocked tool=%s gate=%s reason=%s",
                tool_name,
                outcome.blocked_by,
                outcome.reason,
            )
            if self.security_intel is not None:
                await self.security_intel.push_decision(tool_name, "deny", reason=blocked)
            return blocked, True

        intel = None
        if self.security_intel is not None:
            intel = await self.security_intel.fetch_risk(tool_name, outcome.args)
            if intel is not None:
                self._emit(
                    "capability",
                    "security_intel",
                    {
                        "tool": tool_name,
                        "score": intel.score,
                        "risk_class": intel.risk_class,
                        "hints": intel.policy_hints,
                    },
                    ctx,
                )
                if self.security_intel.enforce_hints and "deny" in intel.policy_hints:
                    logger.warning(
                        "intel hint deny enforced tool=%s hints=%s", tool_name, intel.policy_hints
                    )
                    if self.security_intel is not None:
                        await self.security_intel.push_decision(
                            tool_name, "deny", reason="intel hint deny enforced"
                        )
                    return f"Error: tool '{tool_name}' denied by security intel hint", True

        try:
            tier_decision = None
            if self.sandbox_policy is not None:
                risk = score_tool_risk(tool_name, outcome.args)
                tier_decision = self.sandbox_policy.decide(tool_name, outcome.args, risk)
                if tier_decision.tier is ExecutionTier.BLOCKED:
                    logger.info(
                        "funnel sandbox-blocked tool=%s reason=%s",
                        tool_name,
                        tier_decision.reason,
                    )
                    return (
                        f"Error: sandbox policy blocked tool '{tool_name}': {tier_decision.reason}",
                        True,
                    )
            if tier_decision is not None and tier_decision.tier is ExecutionTier.SANDBOX:
                logger.info("funnel routes tool=%s via sandbox runner", tool_name)
                result = await self.sandbox_policy.runner.run_sandboxed(tool_name, outcome.args)
            else:
                result = await executor(outcome.args)
        except Exception as e:
            result = f"Error: {e}"
        result_str = str(result)
        is_error = result_str.startswith("Error:")
        self._executions += 1
        if self.security_intel is not None:
            await self.security_intel.push_decision(tool_name, "allow", reason=result_str[:200])
        await self.record_result(ctx, tool_name, args, result_str, is_error, node_label)
        return result_str, is_error

    async def record_result(
        self, ctx, tool_name: str, args: dict, result: str, is_error: bool, node_label: str = ""
    ) -> None:
        payload = {"tool_name": tool_name, "args": args, "result": result}
        if self.hooks is not None:
            try:
                await self.hooks.fire("POST_TOOL_USE", payload, tool_name=tool_name)
            except Exception as e:
                logger.warning("post hook error tool=%s err=%s", tool_name, e)
        self._emit(
            "capability",
            "tool_after",
            {
                "tool": tool_name,
                "node": node_label,
                "is_error": is_error,
                "result_preview": (result or "")[:200],
            },
            ctx,
        )


def _budget_exceeded(budget, graph_id: str = "") -> bool:
    try:
        return bool(budget.is_exceeded(graph_id))
    except TypeError:
        # Legacy/stub budgets only implement the zero-arg form.
        return bool(budget.is_exceeded())


class CacheTelemetry:
    """KV-cache axis for funnel accounting (issue #76). Splits input tokens
    into cached vs uncached from provider usage payloads, tracks hit rate
    per run and in aggregate, and counts prompt-prefix breaks (append-only
    violations) with the first divergent message index — answering 'which
    segment broke the cache'. Zero provider API calls: usage payloads and
    request construction only."""

    _CACHED_KEYS = (
        "cache_read_input_tokens",
        "cache_read_tokens",
        "cached_tokens",
    )

    def __init__(self, max_break_events: int = 16) -> None:
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.prefix_steps = 0
        self.prefix_breaks = 0
        self.break_events = []
        self._max_break_events = max_break_events
        self._run_input = 0
        self._run_cached = 0
        self._run_steps = 0
        self._run_breaks = 0
        self._prev_prefix = None

    @staticmethod
    def _extract(usage: dict) -> tuple:
        """(total_input, cached) from a provider usage payload. Anthropic
        style: input_tokens excludes cache reads, so total = uncached +
        cached. OpenAI style: prompt_tokens already includes cached_tokens,
        so total = prompt_tokens."""
        if not isinstance(usage, dict):
            return 0, 0
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0) or 0)
        if not cached:
            for key in CacheTelemetry._CACHED_KEYS:
                if usage.get(key):
                    cached = int(usage[key] or 0)
                    break
        uncached = int(usage.get("prompt_tokens", 0) or 0) or int(usage.get("input_tokens", 0) or 0)
        anthropic_style = any(k in usage for k in ("cache_read_input_tokens", "cache_read_tokens"))
        total = uncached + cached if anthropic_style else uncached
        return total, cached

    def record_usage(self, usage: dict | None) -> dict:
        total, cached = self._extract(usage or {})
        self.input_tokens += total
        self.cached_input_tokens += cached
        self._run_input += total
        self._run_cached += cached
        return {"input_tokens": total, "cached_input_tokens": cached}

    @staticmethod
    def _prefix_key(message: dict) -> str:
        import hashlib
        import json as _json

        role = message.get("role", "")
        content = message.get("content")
        try:
            blob = _json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            blob = str(content)
        return hashlib.sha1(("%s\x00%s" % (role, blob)).encode("utf-8")).hexdigest()

    def observe_prefix(self, messages: list | None) -> dict | None:
        """Append-only check on the outgoing message list: a step is stable
        when the previous list is a strict prefix of the current one.
        Returns the break event dict on a break, else None."""
        keys = [self._prefix_key(m) for m in (messages or [])]
        self.prefix_steps += 1
        self._run_steps += 1
        if self._prev_prefix is None:
            self._prev_prefix = keys
            return None
        prev = self._prev_prefix
        self._prev_prefix = keys
        stable = len(keys) >= len(prev) and keys[: len(prev)] == prev
        if stable:
            return None
        diverge = min(len(prev), len(keys))
        for i in range(min(len(prev), len(keys))):
            if prev[i] != keys[i]:
                diverge = i
                break
        self.prefix_breaks += 1
        self._run_breaks += 1
        event = {"step": self.prefix_steps, "diverge_index": diverge}
        self.break_events.append(event)
        if len(self.break_events) > self._max_break_events:
            self.break_events = self.break_events[-self._max_break_events :]
        logger.info("prefix break step=%d diverge_index=%d", self.prefix_steps, diverge)
        return event

    def start_run(self) -> None:
        self._run_input = 0
        self._run_cached = 0
        self._run_steps = 0
        self._run_breaks = 0

    @staticmethod
    def _rate(num: int, den: int) -> float:
        return round(num / den, 6) if den else 0.0

    def run_snapshot(self) -> dict:
        return {
            "input_tokens": self._run_input,
            "cached_input_tokens": self._run_cached,
            "hit_rate": self._rate(self._run_cached, self._run_input),
            "prefix_steps": self._run_steps,
            "prefix_breaks": self._run_breaks,
            "prefix_stability": self._rate(self._run_steps - self._run_breaks, self._run_steps),
        }

    def snapshot(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "hit_rate": self._rate(self.cached_input_tokens, self.input_tokens),
            "prefix_steps": self.prefix_steps,
            "prefix_breaks": self.prefix_breaks,
            "prefix_stability": self._rate(
                self.prefix_steps - self.prefix_breaks, self.prefix_steps
            ),
            "break_events": list(self.break_events),
        }


class LLMFunnel:
    """Single LLM admission path: lifecycle -> hooks -> budget -> compaction."""

    def __init__(
        self,
        hooks=None,
        lifecycle_bus: LifecycleBus | None = None,
        compactor=None,
        telemetry_engine=None,
        budget_ref=None,
    ):
        self.hooks = hooks
        self.lifecycle = lifecycle_bus or LifecycleBus()
        self.compactor = compactor
        self.telemetry_engine = telemetry_engine
        self.budget_ref = budget_ref
        self.calls = 0
        self.cache = CacheTelemetry()

    def set_budget_ref(self, budget_ref) -> None:
        self.budget_ref = budget_ref

    async def pre_llm(
        self, ctx, node_label: str = "", model: str = "", messages: list | None = None
    ) -> tuple[bool, str, list]:
        """Gate the call. Returns (proceed, reason, possibly-compacted messages)."""
        messages = messages or []
        self._emit(
            "agent",
            "llm_before",
            {"node": node_label, "model": model, "messages": len(messages)},
            ctx,
        )
        # N8: hard budget cap — exceed = graceful stop + BUDGET_EXCEED signal,
        # never silent truncation.
        budget = self.budget_ref
        graph_id = getattr(ctx, "graph_id", "") or ""
        if budget is not None and _budget_exceeded(budget, graph_id):
            logger.warning(
                "llm funnel budget exceeded node=%s spent=%s",
                node_label,
                getattr(budget, "spent_tokens", "?"),
            )
            self._emit(
                "agent",
                "budget_exceed",
                {"node": node_label, "spent_tokens": getattr(budget, "spent_tokens", 0)},
                ctx,
            )
            if self.hooks is not None:
                try:
                    await self.hooks.fire(
                        "BUDGET_EXCEED", {"node": node_label, "stage": "pre_llm"}, tool_name=""
                    )
                except Exception as e:
                    logger.warning("budget_exceed hook error: %s", e)
            return False, "token budget exceeded", messages
        if self.hooks is not None:
            try:
                out = await self.hooks.fire(
                    "LLM_BEFORE", {"node": node_label, "model": model}, tool_name=""
                )
                if out is not None and out.decision == "block":
                    return False, f"blocked by hook: {out.reason or 'llm_before'}", messages
            except Exception as e:
                logger.warning("llm funnel hook error: %s", e)
        if self.compactor is not None and messages:
            try:
                level = self.compactor.should_compact(messages)
            except Exception as e:
                logger.warning("compactor.should_compact error: %s", e)
                level = "none"
            if level != "none":
                self._emit(
                    "agent", "compact_before", {"level": level, "messages": len(messages)}, ctx
                )
                success = True
                error = ""
                try:
                    compact_async = getattr(self.compactor, "compact_async", None)
                    if callable(compact_async):
                        messages = await compact_async(messages, level)
                    else:
                        messages = self.compactor.compact(messages, level)
                except Exception as e:
                    # N8: a failed compaction must never look like a successful
                    # one downstream — carry success=false + error on the event.
                    success = False
                    error = str(e)
                    logger.warning("compaction failed, continuing with original: %s", e)
                payload = {"level": level, "messages": len(messages), "success": success}
                if error:
                    payload["error"] = error
                self._emit("agent", "compact_after", payload, ctx)
        # Cache axis: observe the outgoing message list after compaction —
        # a compaction that rewrites history shows up as a prefix break.
        break_event = self.cache.observe_prefix(messages)
        if break_event is not None:
            self._emit("agent", "prefix_break", dict(break_event), ctx)
        return True, "", messages

    def post_llm(
        self,
        ctx,
        node_label: str = "",
        model: str = "",
        usage: dict | None = None,
        content: str = "",
    ) -> None:
        self.calls += 1
        # N8 metering: feed per-call usage into the wired budget.
        budget = self.budget_ref
        if budget is not None and usage:
            graph_id = getattr(ctx, "graph_id", "") or ""
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            try:
                budget.record_usage(prompt_tokens, completion_tokens, graph_id=graph_id)
            except TypeError:
                budget.record_usage(prompt_tokens, completion_tokens)
            except Exception as e:
                logger.warning("budget record_usage failed: %s", e)
        cache_delta = self.cache.record_usage(usage or {})
        self._emit(
            "agent",
            "llm_after",
            {
                "node": node_label,
                "model": model,
                "usage": usage or {},
                "cache": cache_delta,
                "cache_hit_rate": self.cache.run_snapshot()["hit_rate"],
            },
            ctx,
        )
        if self.hooks is not None:
            try:
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(
                        self.hooks.fire(
                            "LLM_AFTER",
                            {"node": node_label, "model": model, "usage": usage or {}},
                            tool_name="",
                        )
                    )
                else:
                    loop.run_until_complete(
                        self.hooks.fire(
                            "LLM_AFTER",
                            {"node": node_label, "model": model, "usage": usage or {}},
                            tool_name="",
                        )
                    )
            except Exception as e:
                logger.warning("llm_after hook error: %s", e)

    def _emit(self, domain, event: str, payload: dict, ctx) -> None:
        try:
            self.lifecycle.emit(
                domain, event, payload, session_id=getattr(ctx, "session_id", "") or ""
            )
        except Exception as e:
            logger.warning("lifecycle emit failed event=%s err=%s", event, e)


__all__ = [
    "CacheTelemetry",
    "EventDomain",
    "FunnelOutcome",
    "LLMFunnel",
    "LifecycleBus",
    "LifecycleEvent",
    "ToolFunnel",
]
