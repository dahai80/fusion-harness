"""Smoke tests for the extracted kernel (#357)."""

from fusion_harness import (
    CompactionConfig,
    Compactor,
    LifecycleBus,
    LLMFunnel,
    PermissionEngine,
    PermissionRule,
    ToolFunnel,
)
from fusion_harness.funnels import EventDomain
from fusion_harness.hooks import HookConfig, HookEngine


def test_import_surface():
    assert LLMFunnel is not None
    assert ToolFunnel is not None
    assert LifecycleBus is not None
    assert PermissionEngine is not None
    assert HookEngine is not None
    assert Compactor is not None


def test_permission_deny_first():
    engine = PermissionEngine()
    rule = PermissionRule(action="deny", tool="shell", description="no shell")
    engine.add_rule(rule)
    decision = engine.evaluate("shell", {})
    assert decision.decision == "deny"


def test_compactor_roundtrip():
    cfg = CompactionConfig(context_window=200, warning_buffer=100, error_buffer=40, manual_buffer=150, keep_recent_rounds=1)
    compactor = Compactor(cfg)
    assert compactor is not None


def test_hook_engine_register():
    engine = HookEngine()
    engine.register(HookConfig(event="llm.call", matcher="*"))
    assert engine._hooks


def test_lifecycle_bus_emit():
    bus = LifecycleBus()
    seen = []
    bus.subscribe(lambda ev: seen.append(ev))
    bus.emit(EventDomain.SESSION, "test.event", {"k": "v"})
    assert seen
