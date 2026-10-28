# Issue #5: five-stage segmented tool pipeline with monotonic guards.
"""Segmented tool execution: pre-execute waterfall, monotonic guards,
one-shot fail-closed approval, around-dispatch, post-execute waterfall.

Invariants (issue #5):
- Guard stage always runs after the pre-execute waterfall and before the
  tool body; guards are identity-protected (immutable tuple, no setter, no
  extension API), so no hook can reorder, skip, or replace them.
- Approval is one-shot and fail-closed: absent or unanswerable responder
  denies and the tool body is never invoked.
- The final ToolResult is frozen; after the pipeline emits it, no stage
  can mutate it (dataclass frozen — mutation raises FrozenInstanceError).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fusion_harness.permissions import PermissionEngine

logger = logging.getLogger(__name__)

GUARD_ALLOW = "allow"
GUARD_DENY = "deny"
GUARD_ABSTAIN = "abstain"


@dataclass(frozen=True)
class ToolResult:
    # Frozen final result — immutable once the pipeline emits it (#5 AC3).
    tool: str
    ok: bool
    output: Any = ""
    error: str = ""
    started_ts: float = 0.0
    finished_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
        }


class PipelineBlocked(Exception):
    pass


@dataclass
class PipelineOutcome:
    allowed: bool
    result: ToolResult | None = None
    reason: str = ""
    decision_log: list = field(default_factory=list)
    blocked_by: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "result": self.result.to_dict() if self.result is not None else None,
            "reason": self.reason,
            "decision_log": self.decision_log,
            "blocked_by": self.blocked_by,
        }


class MonotonicGuard:
    # Deny/abstain decision gate. Identity-protected once handed to the
    # pipeline: the pipeline stores guards as an immutable tuple with no
    # mutation API, and a guard deny is final — no later stage can flip it.
    def __init__(self, name: str, check: Callable[[str, dict], str]):
        self.name = name
        self._check = check

    def check(self, tool_name: str, args: dict) -> str:
        try:
            verdict = self._check(tool_name, args)
        except Exception as exc:
            # A broken guard is a deny, never a silent pass.
            logger.error("guard %s raised, failing closed: %s", self.name, exc)
            return GUARD_DENY
        if verdict not in (GUARD_ALLOW, GUARD_DENY, GUARD_ABSTAIN):
            logger.error("guard %s returned invalid verdict %r, failing closed", self.name, verdict)
            return GUARD_DENY
        return verdict


# post-execute hook contract: fn(result: ToolResult) -> ToolResult
# - return a NEW ToolResult to replace the candidate result
# - raise PipelineBlocked(reason) to block it
# - return the same result (or None) to accept as-is
PostHook = Callable[[ToolResult], "ToolResult | None"]


class ToolPipeline:
    """Single segmented admission+execution path for tool calls."""

    def __init__(
        self,
        *,
        permission_engine: PermissionEngine | None = None,
        pre_hook: Callable[[str, dict], dict | None] | None = None,
        guards: tuple[MonotonicGuard, ...] = (),
        approver: Any = None,
        timeout_s: float | None = None,
        retries: int = 0,
        post_hooks: tuple[PostHook, ...] = (),
        session_log: Any = None,
    ):
        self.permission_engine = permission_engine
        # pre_hook: returns rewritten args dict, None to keep, or raises
        # PipelineBlocked to stop before the guards.
        self.pre_hook = pre_hook
        # Identity-protected: tuple, no setter, no extension API. Hooks get
        # no reference to the pipeline, so they cannot touch this.
        self._guards = tuple(guards)
        # approver: request(tool_name, args) -> True/False/None. None or a
        # missing approver denies (fail-closed), one call, never retried.
        self.approver = approver
        self.timeout_s = timeout_s
        self.retries = retries
        self._post_hooks = tuple(post_hooks)
        self.session_log = session_log
        self.metrics: dict[str, int] = {
            "executed": 0,
            "denied_guard": 0,
            "denied_approval": 0,
            "denied_pre": 0,
            "blocked_post": 0,
            "timeouts": 0,
            "retries": 0,
        }

    @property
    def guards(self) -> tuple[MonotonicGuard, ...]:
        return self._guards

    async def run(self, ctx: Any = None, tool_name: str = "", args: dict | None = None,
                  executor: Callable[[dict], Any] | None = None,
                  node_label: str = "") -> PipelineOutcome:
        args = dict(args or {})
        log: list[dict] = []

        def _mark(stage: str, verdict: str, reason: str = "") -> None:
            log.append({"stage": stage, "verdict": verdict, "reason": reason})

        # Stage 1: pre-execute waterfall (hook, policy).
        if self.pre_hook is not None:
            try:
                rewritten = self.pre_hook(tool_name, args)
            except PipelineBlocked as exc:
                self.metrics["denied_pre"] += 1
                _mark("pre", "deny", str(exc))
                return self._deny("pre", str(exc), log)
            if rewritten is not None:
                args = dict(rewritten)
                _mark("pre", "rewrite", "args rewritten by pre hook")
            else:
                _mark("pre", "pass", "")
        approval_required = False
        if self.permission_engine is not None:
            decision = self.permission_engine.evaluate(tool_name, args, ctx=ctx)
            _mark("policy", decision.decision, decision.reason)
            if decision.decision == "deny":
                self.metrics["denied_pre"] += 1
                return self._deny("policy", decision.reason, log)
            if decision.decision == "ask":
                approval_required = True

        # Stage 2: monotonic guards — always run, deny is final.
        for guard in self._guards:
            verdict = guard.check(tool_name, args)
            _mark(f"guard:{guard.name}", verdict, "")
            if verdict == GUARD_DENY:
                self.metrics["denied_guard"] += 1
                logger.info("guard %s denied tool=%s (final)", guard.name, tool_name)
                return self._deny(f"guard:{guard.name}", f"denied by guard {guard.name}", log)

        # Stage 3: one-shot fail-closed approval.
        if approval_required:
            if self.approver is None:
                self.metrics["denied_approval"] += 1
                _mark("approval", "deny", "no approval responder wired (fail-closed)")
                return self._deny("approval", "approval required but no responder wired", log)
            try:
                answer = self.approver.request(tool_name, args)
            except Exception as exc:
                logger.warning("approval responder error tool=%s: %s", tool_name, exc)
                answer = None
            if answer is not True:
                self.metrics["denied_approval"] += 1
                _mark("approval", "deny", "unanswered or declined (fail-closed)")
                return self._deny("approval", "approval declined or unanswerable", log)
            _mark("approval", "allow", "approved by responder")

        # Stage 4: around-dispatch (timeout, retry, metrics).
        if executor is None:
            return self._deny("dispatch", "no executor wired", log)
        started = time.time()
        result = await self._dispatch(executor, tool_name, args)
        self.metrics["executed"] += 1

        # Stage 5: post-execute waterfall (accept / block / replace).
        for hook in self._post_hooks:
            try:
                replacement = hook(result)
            except PipelineBlocked as exc:
                self.metrics["blocked_post"] += 1
                _mark("post", "block", str(exc))
                return self._deny("post", str(exc), log)
            if replacement is not None and replacement is not result:
                _mark("post", "replace", "candidate result replaced")
                result = replacement
            else:
                _mark("post", "accept", "")

        # Freeze + emit: the emitted tool/result event is immutable.
        final = ToolResult(
            tool=tool_name,
            ok=result.ok,
            output=result.output,
            error=result.error,
            started_ts=started,
            finished_ts=time.time(),
        )
        _mark("emit", "frozen", "")
        if self.session_log is not None:
            try:
                self.session_log.append("tool_result", {"tool": tool_name, "ok": final.ok,
                                                        "output": final.output, "error": final.error})
            except Exception as exc:
                logger.warning("session log append failed tool=%s: %s", tool_name, exc)
        return PipelineOutcome(allowed=final.ok, result=final, decision_log=log,
                               reason="" if final.ok else final.error)

    async def _dispatch(self, executor: Callable[[dict], Any], tool_name: str, args: dict) -> ToolResult:
        attempts = self.retries + 1
        last_error = ""
        for attempt in range(attempts):
            if attempt > 0:
                self.metrics["retries"] += 1
                logger.info("dispatch retry tool=%s attempt=%d", tool_name, attempt + 1)
            try:
                out = executor(args)
                if inspect.isawaitable(out):
                    out = await (asyncio.wait_for(out, self.timeout_s) if self.timeout_s else out)
                return ToolResult(tool=tool_name, ok=True, output=out)
            except asyncio.TimeoutError:
                self.metrics["timeouts"] += 1
                last_error = f"timeout after {self.timeout_s}s"
                logger.warning("dispatch timeout tool=%s attempt=%d", tool_name, attempt + 1)
            except Exception as exc:
                last_error = str(exc)
                logger.warning("dispatch error tool=%s attempt=%d: %s", tool_name, attempt + 1, exc)
        return ToolResult(tool=tool_name, ok=False, error=last_error)

    def _deny(self, blocked_by: str, reason: str, log: list) -> PipelineOutcome:
        logger.info("pipeline denied blocked_by=%s reason=%s", blocked_by, reason)
        return PipelineOutcome(allowed=False, result=None, reason=reason,
                               decision_log=log, blocked_by=blocked_by)


__all__ = [
    "GUARD_ABSTAIN",
    "GUARD_ALLOW",
    "GUARD_DENY",
    "MonotonicGuard",
    "PipelineBlocked",
    "PipelineOutcome",
    "ToolPipeline",
    "ToolResult",
]
