"""Funnel unit coverage: issue #1 fail-closed default + issue #2 R3."""

import logging

import pytest

from fusion_harness.funnels import EventDomain, LifecycleBus, ToolFunnel
from fusion_harness.permissions import PermissionEngine, PermissionRule


def _engine() -> PermissionEngine:
    return PermissionEngine()


@pytest.mark.asyncio
async def test_absent_permission_engine_denies_by_default(monkeypatch):
    # Issue #1 AC1: no env set -> fail-closed, even without a dangerous arg.
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)
    funnel = ToolFunnel()
    outcome = await funnel.admit(tool_name="shell", args={"command": "ls"})
    assert not outcome.allowed
    assert outcome.blocked_by == "permission"
    assert "fail-closed" in outcome.reason


@pytest.mark.asyncio
async def test_opt_out_permits_and_logs_warning(monkeypatch, caplog):
    # Issue #1 AC2: explicit opt-out -> permitted + prominent warning logged.
    monkeypatch.setenv("FUSION_FUNNEL_PERMISSIONS", "off")
    caplog.set_level(logging.WARNING, logger="fusion_harness.funnels")
    funnel = ToolFunnel()
    outcome = await funnel.admit(tool_name="shell", args={"command": "ls"})
    assert outcome.allowed
    assert any(
        "permission enforcement DISABLED via FUSION_FUNNEL_PERMISSIONS" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_opt_out_via_legacy_var_permits(monkeypatch, caplog):
    monkeypatch.setenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", "0")
    caplog.set_level(logging.WARNING, logger="fusion_harness.funnels")
    funnel = ToolFunnel()
    outcome = await funnel.admit(tool_name="shell", args={"command": "ls"})
    assert outcome.allowed
    assert any("DISABLED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_opt_out_bypasses_engine_entirely(monkeypatch, caplog):
    # Issue #1 AC2: opt-out disables funnel-level enforcement even with a
    # wired engine (headless deployments trust their own tool layer). The
    # prominent warning is the safety valve.
    monkeypatch.setenv("FUSION_FUNNEL_PERMISSIONS", "off")
    caplog.set_level(logging.WARNING, logger="fusion_harness.funnels")
    funnel = ToolFunnel(permissions=_engine())
    outcome = await funnel.admit(
        tool_name="shell", args={"command": "rm -rf /"}
    )
    assert outcome.allowed
    assert any("DISABLED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dangerous_pattern_denied_with_engine_wired(monkeypatch):
    # Issue #1 AC1: no env, engine wired, dangerous pattern -> denied.
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)
    funnel = ToolFunnel(permissions=_engine())
    outcome = await funnel.admit(tool_name="shell", args={"command": "rm -rf /"})
    assert not outcome.allowed
    assert outcome.blocked_by == "permission"
    assert "dangerous pattern" in outcome.reason


@pytest.mark.asyncio
async def test_rule_deny_blocks_admission():
    engine = _engine()
    engine.add_rule(PermissionRule(action="deny", tool="shell"))
    funnel = ToolFunnel(permissions=engine)
    outcome = await funnel.admit(tool_name="shell", args={"command": "ls"})
    assert not outcome.allowed
    assert outcome.blocked_by == "permission"


@pytest.mark.asyncio
async def test_readonly_tool_passes_unarmed_engine():
    engine = _engine()
    funnel = ToolFunnel(permissions=engine, readonly_tools={"read_file"})
    outcome = await funnel.admit(tool_name="read_file", args={"path": "/tmp/x"})
    assert outcome.allowed


@pytest.mark.asyncio
async def test_run_tool_executes_when_allowed(monkeypatch):
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)

    async def exec_ok(args):
        return "done"

    funnel = ToolFunnel(permissions=_engine())
    funnel.permissions.add_rule(PermissionRule(action="allow", tool="echo_tool"))
    result_str, is_error = await funnel.run_tool(None, "echo_tool", {"x": 1}, exec_ok)
    assert result_str == "done"
    assert not is_error
    assert funnel._executions == 1


@pytest.mark.asyncio
async def test_run_tool_blocked_returns_error(monkeypatch):
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)
    funnel = ToolFunnel()  # no engine -> fail-closed deny
    result_str, is_error = await funnel.run_tool(None, "shell", {}, lambda a: "nope")
    assert is_error
    assert "Blocked by permission" in result_str


class _RaisingCompactor:
    def should_compact(self, messages):
        return "warning"

    def compact(self, messages, level):
        raise RuntimeError("mlx down")


class _OkCompactor:
    def should_compact(self, messages):
        return "warning"

    def compact(self, messages, level):
        return messages[:1]


class _Ctx:
    session_id = "s-compact"


@pytest.mark.asyncio
async def test_pre_llm_compaction_failure_emits_success_false():
    # Issue #7: a failed compaction must surface success=false + error on
    # compact_after, and the original messages must pass through unchanged.
    from fusion_harness.funnels import LLMFunnel

    funnel = LLMFunnel(compactor=_RaisingCompactor())
    msgs = [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}]
    proceed, reason, out = await funnel.pre_llm(_Ctx(), "n1", "m1", msgs)
    assert proceed and reason == ""
    assert out == msgs  # original messages preserved
    after = [
        e for e in funnel.lifecycle.replay(session_id="s-compact") if e.event == "compact_after"
    ]
    assert len(after) == 1
    assert after[0].payload["success"] is False
    assert "mlx down" in after[0].payload["error"]


@pytest.mark.asyncio
async def test_pre_llm_compaction_success_carries_success_true():
    from fusion_harness.funnels import LLMFunnel

    funnel = LLMFunnel(compactor=_OkCompactor())
    proceed, reason, out = await funnel.pre_llm(
        _Ctx(), "n1", "m1", [{"role": "user", "content": "x"}]
    )
    assert proceed and reason == ""
    assert len(out) == 1
    after = [
        e for e in funnel.lifecycle.replay(session_id="s-compact") if e.event == "compact_after"
    ]
    assert len(after) == 1
    assert after[0].payload["success"] is True
    assert "error" not in after[0].payload


def test_lifecycle_bus_subscribe_emit_replay():
    bus = LifecycleBus()
    seen = []
    bus.subscribe(seen.append)
    bus.emit(EventDomain.SESSION, "session.start", {"sid": "s1"}, session_id="s1")
    bus.emit(EventDomain.CAPABILITY, "tool.before", {"t": "shell"}, session_id="s1")
    assert len(seen) == 2
    replayed = bus.replay(session_id="s1")
    assert len(replayed) == 2
    assert replayed[0].event == "session.start"


def test_lifecycle_bus_unsubscribe():
    bus = LifecycleBus()
    seen = []
    bus.subscribe(seen.append)
    bus.unsubscribe(seen.append)
    bus.emit(EventDomain.AGENT, "agent.tick", {})
    assert seen == []


def test_lifecycle_bus_jsonl_persistence(tmp_path):
    import json

    bus = LifecycleBus(jsonl_dir=str(tmp_path))
    bus.emit(EventDomain.SESSION, "session.start", {"sid": "s1"}, session_id="s1")
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines() if line]
    assert rows and rows[0]["event"] == "session.start"


def test_lifecycle_bus_session_count():
    bus = LifecycleBus()
    bus.emit(EventDomain.SESSION, "e1", {}, session_id="a")
    bus.emit(EventDomain.SESSION, "e2", {}, session_id="a")
    bus.emit(EventDomain.SESSION, "e3", {}, session_id="b")
    assert bus.session_count("a") == 2
    assert bus.session_count("b") == 1
