"""Hook engine unit coverage (issue #2 R3)."""

import asyncio

import pytest

from fusion_harness.hooks import (
    FAIL_CLOSED_EVENTS,
    HookConfig,
    HookEngine,
    HookResult,
    hook,
)


def _callback_hook(event="POST_TOOL_USE", matcher="*", fn=lambda p: HookResult()):
    return HookConfig(event=event, matcher=matcher, type="callback", callback=fn)


def test_register_and_fire_no_match():
    engine = HookEngine(auto_register_decorators=False)
    engine.register(_callback_hook(event="SESSION_START"))
    result = asyncio.run(engine.fire("SESSION_END", {}))
    assert result.continue_loop
    assert result.decision == "approve"


def test_callback_hook_fires_and_aggregates():
    engine = HookEngine(auto_register_decorators=False)
    engine.register(
        _callback_hook(
            fn=lambda p: HookResult(
                decision="block", reason="blocked by test", continue_loop=False
            )
        )
    )
    result = asyncio.run(engine.fire("POST_TOOL_USE", {"tool_name": "x"}))
    assert result.decision == "block"
    assert result.reason == "blocked by test"
    assert not result.continue_loop


def test_block_decision_keeps_loop_flag_independent():
    engine = HookEngine(auto_register_decorators=False)
    engine.register(
        _callback_hook(fn=lambda p: HookResult(decision="block", reason="just a veto"))
    )
    result = asyncio.run(engine.fire("POST_TOOL_USE", {"tool_name": "x"}))
    assert result.decision == "block"
    assert result.continue_loop  # aggregation copies the flag verbatim


def test_matcher_filters_by_tool_name():
    engine = HookEngine(auto_register_decorators=False)
    engine.register(_callback_hook(matcher="shell*", fn=lambda p: HookResult(decision="block")))
    hit = asyncio.run(engine.fire("POST_TOOL_USE", {}, tool_name="shell_exec"))
    miss = asyncio.run(engine.fire("POST_TOOL_USE", {}, tool_name="read_file"))
    assert hit.decision == "block"
    assert miss.decision == "approve"


@pytest.mark.parametrize("event", sorted(FAIL_CLOSED_EVENTS))
def test_fail_closed_events_block_on_callback_error(event):
    engine = HookEngine(auto_register_decorators=False)

    def boom(payload):
        raise RuntimeError("hook crashed")

    engine.register(_callback_hook(event=event, fn=boom))
    result = asyncio.run(engine.fire(event, {"tool_name": "x"}))
    assert result.decision == "block"
    assert not result.continue_loop
    assert "fail-closed" in result.reason


def test_non_security_event_swallows_callback_error():
    engine = HookEngine(auto_register_decorators=False)

    def boom(payload):
        raise RuntimeError("hook crashed")

    engine.register(_callback_hook(event="SESSION_START", fn=boom))
    result = asyncio.run(engine.fire("SESSION_START", {}))
    assert result.decision == "approve"


def test_chain_budget_exceeded_blocks_fail_closed_event():
    engine = HookEngine(auto_register_decorators=False, max_fire_ms=0.0)
    engine.register(_callback_hook(event="PRE_TOOL_USE"))
    result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool_name": "x"}))
    assert result.decision == "block"
    assert "fail-closed" in result.reason


def test_command_hook_blocked_without_env(monkeypatch):
    monkeypatch.delenv("FUSION_HOOKS_ALLOW_COMMAND", raising=False)
    engine = HookEngine(auto_register_decorators=False)
    engine.register(
        HookConfig(event="PRE_TOOL_USE", type="command", command=["echo", "hi"])
    )
    result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool_name": "x"}))
    assert result.decision == "block"
    assert "disabled" in result.reason


def test_decorator_registry_flush():
    engine = HookEngine(auto_register_decorators=False)

    @hook("SESSION_END", matcher="*")
    def on_session_end(payload):
        return HookResult()

    n = engine.flush_decorator_registry()
    assert n == 1
    result = asyncio.run(engine.fire("SESSION_END", {}))
    assert result.decision == "approve"


def test_list_hooks_reports_registered():
    engine = HookEngine(auto_register_decorators=False)
    engine.register(_callback_hook(event="SESSION_START"))
    entries = engine.list_hooks()
    assert any(e["event"] == "SESSION_START" for e in entries)
