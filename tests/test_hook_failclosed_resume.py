"""Tests for issue #89: hook fail-closed semantics per hook kind, and
HumanGate deferred-approval persistence across resume.

Hook rule: a hook whose CALL fails (exception, timeout, non-JSON output,
disabled command execution) must block on security-gate events — never
fail open. HumanGate rule: a deferred (pending) approval question survives
a host restart via the persistent queue; an approval answered before the
restart resolves exactly once after it; nothing re-executes silently."""

import asyncio
import json

import pytest

from fusion_harness.hooks import FAIL_CLOSED_EVENTS, HookConfig, HookEngine
from fusion_harness.human_gate import HumanGate


def _engine(tmp_path, hooks, allow_command=False):
    engine = HookEngine(auto_register_decorators=False)
    for h in hooks:
        engine.register(h)
    if allow_command:
        import os

        os.environ["FUSION_HOOKS_ALLOW_COMMAND"] = "1"
    return engine


@pytest.fixture
def allow_command():
    import os

    os.environ["FUSION_HOOKS_ALLOW_COMMAND"] = "1"
    yield
    os.environ.pop("FUSION_HOOKS_ALLOW_COMMAND", None)


class TestHookFailClosedByKind:
    @pytest.mark.parametrize("event", sorted(FAIL_CLOSED_EVENTS))
    def test_callback_exception_blocks(self, tmp_path, event):
        def boom(payload):
            raise RuntimeError("hook exploded")

        engine = HookEngine(auto_register_decorators=False)
        engine.register(HookConfig(type="callback", event=event, callback=boom))
        result = asyncio.run(engine.fire(event, {}))
        assert result.decision == "block"
        assert "fail-closed" in result.reason

    def test_command_timeout_blocks_on_security_event(self, tmp_path, allow_command):
        hook = HookConfig(
            type="command",
            event="PRE_TOOL_USE",
            command="sleep 5",
            timeout=0.2,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool": "x"}))
        assert result.decision == "block"
        assert "timed out" in result.reason

    def test_command_non_json_output_blocks(self, tmp_path, allow_command):
        hook = HookConfig(
            type="command",
            event="PRE_TOOL_USE",
            command="echo not-json",
            timeout=5,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool": "x"}))
        assert result.decision == "block"
        assert "non-JSON" in result.reason

    def test_command_nonzero_exit_blocks(self, tmp_path, allow_command):
        hook = HookConfig(
            type="command",
            event="PRE_TOOL_USE",
            command="exit 3",
            timeout=5,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool": "x"}))
        assert result.decision == "block"

    def test_disabled_command_blocks_on_security_event(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FUSION_HOOKS_ALLOW_COMMAND", raising=False)
        hook = HookConfig(
            type="command",
            event="PRE_TOOL_USE",
            command="echo approve",
            timeout=5,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool": "x"}))
        assert result.decision == "block"

    def test_command_approval_still_works_when_enabled(self, tmp_path, allow_command):
        hook = HookConfig(
            type="command",
            event="PRE_TOOL_USE",
            command='echo \'{"decision": "approve"}\'',
            timeout=5,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("PRE_TOOL_USE", {"tool": "x"}))
        assert result.decision != "block"

    def test_non_security_event_stays_fail_open(self, tmp_path, allow_command):
        hook = HookConfig(
            type="command",
            event="SESSION_START",
            command="echo not-json",
            timeout=5,
        )
        engine = HookEngine(auto_register_decorators=False)
        engine.register(hook)
        result = asyncio.run(engine.fire("SESSION_START", {}))
        assert result.decision != "block"


class TestHumanGateDeferredResume:
    def _gate(self, tmp_path):
        return HumanGate(state_dir=str(tmp_path / "hg"))

    def test_pending_question_survives_restart(self, tmp_path):
        gate = self._gate(tmp_path)
        q = gate.ask(
            "sess-1",
            "approve rm -rf /tmp/old?",
            reply_schema={"approved": {"type": "boolean"}},
            requester="host",
        )
        # Host restart: fresh instance over the same state dir.
        revived = HumanGate(state_dir=str(tmp_path / "hg"))
        pending = revived.pending(session_id="sess-1")
        assert [p.id for p in pending] == [q.id]

    def test_deferred_call_answered_after_restart(self, tmp_path):
        gate = self._gate(tmp_path)
        q = gate.ask(
            "sess-1",
            "approve?",
            reply_schema={"approved": {"type": "boolean"}},
        )
        revived = HumanGate(state_dir=str(tmp_path / "hg"))
        revived.reply(q.id, {"approved": True})

        async def wait():
            return await revived.wait(q.id, timeout=1)

        reply = asyncio.run(wait())
        assert reply == {"approved": True}

    def test_answer_before_restart_resolves_after(self, tmp_path):
        gate = self._gate(tmp_path)
        q = gate.ask("sess-1", "approve?")
        gate.reply(q.id, {"approved": False})
        revived = HumanGate(state_dir=str(tmp_path / "hg"))

        async def wait():
            return await revived.wait(q.id, timeout=1)

        reply = asyncio.run(wait())
        assert reply == {"approved": False}

    def test_answered_question_consumed_once(self, tmp_path):
        gate = self._gate(tmp_path)
        q = gate.ask("sess-1", "approve?")
        gate.reply(q.id, {"approved": True})
        revived = HumanGate(state_dir=str(tmp_path / "hg"))
        assert revived.take_reply(q.id) == {"approved": True}
        assert revived.take_reply(q.id) is None

    def test_expired_deferred_call_is_dropped_not_executed(self, tmp_path):
        clock = {"now": 1000.0}
        gate = HumanGate(state_dir=str(tmp_path / "hg2"), clock=lambda: clock["now"])
        q = gate.ask("sess-1", "approve?", ttl_s=10)
        clock["now"] = 2000.0
        revived = HumanGate(state_dir=str(tmp_path / "hg2"), clock=lambda: clock["now"])

        async def wait():
            return await revived.wait(q.id, timeout=0.5)

        assert asyncio.run(wait()) is None
        assert revived.pending() == []

    def test_corrupt_queue_entry_skipped_not_fatal(self, tmp_path):
        state_dir = tmp_path / "hg3"
        state_dir.mkdir(parents=True)
        (state_dir / "human_gate_queue.json").write_text(
            json.dumps([{"id": "hq_bad", "bogus": "shape"}]), encoding="utf-8"
        )
        gate = HumanGate(state_dir=str(state_dir))
        assert gate.pending() == []
