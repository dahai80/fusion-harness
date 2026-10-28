"""Segmented pipeline coverage: issue #5 (guards + fail-closed approval + frozen results)."""

import dataclasses

import pytest

from fusion_harness.pipeline import (
    GUARD_ABSTAIN,
    GUARD_ALLOW,
    GUARD_DENY,
    MonotonicGuard,
    PipelineBlocked,
    ToolPipeline,
    ToolResult,
)
from fusion_harness.permissions import ConfirmationPolicy, PermissionEngine, PermissionRule


def _engine_armed():
    engine = PermissionEngine()
    engine.add_rule(PermissionRule(action="allow", tool="safe_tool"))
    engine.add_rule(PermissionRule(action="ask", tool="risky_tool"))
    return engine


def _deny_tool_guard():
    return MonotonicGuard("no_risky", lambda tool, args: GUARD_DENY if tool == "risky_tool" else GUARD_ABSTAIN)


@pytest.mark.asyncio
async def test_happy_path_runs_executor():
    pipeline = ToolPipeline(permission_engine=_engine_armed())
    out = await pipeline.run(tool_name="safe_tool", args={"x": 1}, executor=lambda a: a["x"] * 2)
    assert out.allowed
    assert out.result.ok and out.result.output == 2
    stages = [e["stage"] for e in out.decision_log]
    assert "policy" in stages and "guard" not in stages


@pytest.mark.asyncio
async def test_pre_hook_cannot_bypass_guard_deny():
    # AC1: a hook that rewrites args "safe" still cannot bypass the guard —
    # guards run after the waterfall, unconditionally, and deny is final.
    def sneaky_hook(tool, args):
        return {"x": 999, "disguised": True}

    pipeline = ToolPipeline(
        permission_engine=_engine_armed(),
        pre_hook=sneaky_hook,
        guards=(_deny_tool_guard(),),
    )
    out = await pipeline.run(tool_name="risky_tool", args={"x": 1}, executor=lambda a: "ran")
    assert not out.allowed
    assert out.blocked_by == "guard:no_risky"
    assert out.result is None  # tool body never dispatched
    guard_marks = [e for e in out.decision_log if e["stage"].startswith("guard:")]
    assert guard_marks and guard_marks[0]["verdict"] == GUARD_DENY


@pytest.mark.asyncio
async def test_guards_are_identity_protected():
    pipeline = ToolPipeline(guards=(_deny_tool_guard(),))
    assert isinstance(pipeline.guards, tuple)
    with pytest.raises((AttributeError, TypeError)):
        pipeline.guards = ()
    with pytest.raises((AttributeError, TypeError)):
        pipeline.guards.append(MonotonicGuard("late", lambda t, a: GUARD_ALLOW))
    assert len(pipeline.guards) == 1


@pytest.mark.asyncio
async def test_broken_guard_fails_closed():
    def boom(tool, args):
        raise RuntimeError("guard bug")

    pipeline = ToolPipeline(guards=(MonotonicGuard("broken", boom),))
    out = await pipeline.run(tool_name="safe_tool", args={}, executor=lambda a: "ran")
    assert not out.allowed
    assert out.blocked_by == "guard:broken"


@pytest.mark.asyncio
async def test_no_approver_denies_and_skips_body():
    # AC2: policy says ask, no responder wired -> deny, executor untouched.
    called = []
    pipeline = ToolPipeline(permission_engine=_engine_armed())
    out = await pipeline.run(
        tool_name="risky_tool", args={}, executor=lambda a: called.append(a) or "ran"
    )
    assert not out.allowed
    assert out.blocked_by == "approval"
    assert called == []


@pytest.mark.asyncio
async def test_unanswerable_approver_denies():
    class Unanswerable:
        def request(self, tool, args):
            return None  # timeout / no human

    called = []
    pipeline = ToolPipeline(permission_engine=_engine_armed(), approver=Unanswerable())
    out = await pipeline.run(
        tool_name="risky_tool", args={}, executor=lambda a: called.append(a) or "ran"
    )
    assert not out.allowed and out.blocked_by == "approval"
    assert called == []


@pytest.mark.asyncio
async def test_approved_call_runs_once():
    calls = []

    class Approver:
        def __init__(self):
            self.requests = 0

        def request(self, tool, args):
            self.requests += 1
            return True

    approver = Approver()
    pipeline = ToolPipeline(permission_engine=_engine_armed(), approver=approver)
    out = await pipeline.run(
        tool_name="risky_tool", args={}, executor=lambda a: calls.append(1) or "ran"
    )
    assert out.allowed
    assert approver.requests == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_post_hook_replace_and_block():
    def replacer(result):
        return ToolResult(tool=result.tool, ok=True, output="replaced")

    pipeline = ToolPipeline(
        permission_engine=_engine_armed(),
        post_hooks=(replacer,),
    )
    out = await pipeline.run(tool_name="safe_tool", args={}, executor=lambda a: "original")
    assert out.result.output == "replaced"

    def blocker(result):
        raise PipelineBlocked("output leaked secrets")

    pipeline2 = ToolPipeline(permission_engine=_engine_armed(), post_hooks=(blocker,))
    out2 = await pipeline2.run(tool_name="safe_tool", args={}, executor=lambda a: "original")
    assert not out2.allowed and out2.blocked_by == "post"


@pytest.mark.asyncio
async def test_final_result_frozen_immutable():
    # AC3: mutation attempt on the frozen result fails.
    pipeline = ToolPipeline(permission_engine=_engine_armed())
    out = await pipeline.run(tool_name="safe_tool", args={}, executor=lambda a: "data")
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.result.output = "tampered"
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.result.ok = False


@pytest.mark.asyncio
async def test_emitted_event_payload_immutable_copy():
    from fusion_harness.session_log import SessionEventLog

    log = SessionEventLog("s-pipe")
    pipeline = ToolPipeline(permission_engine=_engine_armed(), session_log=log)
    out = await pipeline.run(tool_name="safe_tool", args={}, executor=lambda a: "data")
    event = log.events()[-1]
    assert event.type == "tool_result" and event.payload["output"] == "data"
    # mutating the frozen result afterwards cannot change the emitted event
    payload_snapshot = dict(event.payload)
    assert out.result.to_dict()["output"] == payload_snapshot["output"]


@pytest.mark.asyncio
async def test_timeout_and_retry():
    import asyncio

    attempts = []

    async def slow(a):
        attempts.append(1)
        await asyncio.sleep(0.2)
        return "never"

    pipeline = ToolPipeline(timeout_s=0.02, retries=1)
    out = await pipeline.run(tool_name="safe_tool", args={}, executor=slow)
    assert not out.allowed
    assert "timeout" in out.result.error
    assert len(attempts) == 2
    assert pipeline.metrics["timeouts"] == 2 and pipeline.metrics["retries"] == 1


@pytest.mark.asyncio
async def test_policy_deny_short_circuits_before_guards():
    engine = PermissionEngine()
    engine.add_rule(PermissionRule(action="deny", tool="banned"))
    guard_seen = []

    def tracking_guard(tool, args):
        guard_seen.append(tool)
        return GUARD_ABSTAIN

    pipeline = ToolPipeline(permission_engine=engine, guards=(MonotonicGuard("t", tracking_guard),))
    out = await pipeline.run(tool_name="banned", args={}, executor=lambda a: "ran")
    assert not out.allowed and out.blocked_by == "policy"
    assert guard_seen == []  # deny never even reaches the guard stage


@pytest.mark.asyncio
async def test_confirmation_policy_integrates_via_engine():
    # #9 + #5: tier=risk escalates an auto-passed destructive call; the
    # pipeline's approval stage then honors the escalation fail-closed.
    engine = PermissionEngine(
        confirmation_policy=ConfirmationPolicy(tier="risk", risk_threshold=0.7),
    )
    engine.add_rule(PermissionRule(action="allow", tool="safe_tool"))
    pipeline = ToolPipeline(permission_engine=engine)
    out = await pipeline.run(
        tool_name="safe_tool",
        args={"command": "TRUNCATE TABLE users"},  # explicit rule allow, never escalated
        executor=lambda a: "ran",
    )
    assert out.allowed
