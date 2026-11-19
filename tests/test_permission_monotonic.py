"""Tests for issue #90: permission inheritance monotonicity + sandbox
conformance. A child context derived for a spawned subagent can never be
more permissive than its parent, and parent deny rules are irrevocable
floors. Sandbox conformance: deny without runner, tier routing, reference
runner conformance."""

import os

import pytest

from fusion_harness.permissions import PermissionMode  # noqa: F401
from fusion_harness.sandbox import (
    ExecutionTier,
    SandboxExecRunner,
    SandboxPolicy,
    SandboxRunnerUnavailable,
    check_runner_conformance,
    map_risk_to_tier,
)
from fusion_harness.scoped_permissions import (
    PERMISSIVENESS,
    ScopedPermission,
    ScopedPermissionEngine,
)


class TestInheritNotWeaken:
    def _parent(self):
        engine = ScopedPermissionEngine(level="confirm")
        engine.grant(ScopedPermission(subject="shell_exec", allowed=False))
        return engine

    def test_child_cannot_widen_level(self):
        child = self._parent().child(level="bypass")
        assert child.level == "confirm"

    def test_child_same_or_lower_level_allowed(self):
        parent = self._parent()
        assert parent.child(level="confirm").level == "confirm"
        manual_parent = ScopedPermissionEngine(level="manual")
        assert manual_parent.child(level="confirm").level == "confirm"

    def test_child_unknown_level_rejected(self):
        with pytest.raises(Exception):
            self._parent().child(level="galactic")

    def test_floor_deny_survives_in_child(self):
        child = self._parent().child()
        assert child.check("shell_exec", "/tmp/x") == "deny"

    def test_floor_deny_beats_child_allow(self):
        parent = self._parent()
        child = parent.child(extra_rules=[ScopedPermission(subject="shell_exec", allowed=True)])
        assert child.check("shell_exec", "/tmp/x") == "deny"

    def test_floor_deny_irrevocable(self):
        child = self._parent().child()
        removed = child.revoke("shell_exec")
        assert removed == 0
        assert child.check("shell_exec", "/tmp/x") == "deny"

    def test_child_keeps_parent_high_risk_set(self):
        child = self._parent().child()
        # high-risk without guard -> fail-closed deny + queued approval
        verdict = child.check("file_delete", "/tmp/a")
        assert verdict == "deny"
        assert child.list_pending()

    def test_parent_rules_dont_leak_allows_to_child(self):
        parent = ScopedPermissionEngine(level="confirm")
        parent.grant(ScopedPermission(subject="read_file", allowed=True))
        child = parent.child()
        # allow rules are not copied — child starts from parent level only;
        # low-risk default allow applies, high-risk still guarded.
        assert child.check("read_file", "/tmp/a") == "allow"

    def test_permissiveness_order_documented(self):
        assert PERMISSIVENESS["confirm"] < PERMISSIVENESS["manual"] < PERMISSIVENESS["bypass"]


class TestSandboxConformance:
    def test_sandbox_tier_without_runner_denies(self):
        policy = SandboxPolicy(tool_tiers={"shell_exec": ExecutionTier.SANDBOX})
        decision = policy.decide("shell_exec", {}, (0.5, ""))
        assert decision.tier is ExecutionTier.BLOCKED

    def test_bypass_mode_still_requires_sandbox_for_tiered_tools(self):
        # Engine bypass lets the call through admission, but the sandbox
        # tier routing is independent: no runner wired = blocked, even in
        # bypass mode (bypass only inside isolation).
        policy = SandboxPolicy(tool_tiers={"shell_exec": ExecutionTier.SANDBOX})
        decision = policy.decide("shell_exec", {}, (0.5, ""))
        assert decision.tier is ExecutionTier.BLOCKED
        assert "no runner" in decision.reason

    def test_tier_routing_by_risk_band(self):
        policy = SandboxPolicy(runner=object())
        assert policy.decide("t", {}, (0.95, "destructive")).tier is ExecutionTier.SANDBOX
        assert policy.decide("t", {}, (0.1, "read")).tier is ExecutionTier.PROCESS
        assert policy.decide("t", {}, (0.95, "")).tier is ExecutionTier.SANDBOX

    def test_map_risk_to_tier_bands(self):
        assert map_risk_to_tier(0.1, "destructive") is ExecutionTier.SANDBOX
        assert map_risk_to_tier(0.1, "read") is ExecutionTier.PROCESS

    def test_runner_conformance_reference_runner(self):
        assert check_runner_conformance(SandboxExecRunner()) == []

    def test_runner_conformance_rejects_sync_runner(self):
        class Bad:
            def run_sandboxed(self, tool_name, args):
                return None

        violations = check_runner_conformance(Bad())
        assert violations and "async" in violations[0]

    def test_runner_conformance_rejects_non_runner(self):
        assert check_runner_conformance(object())

    @pytest.mark.skipif(
        not hasattr(os, "uname") or os.uname().sysname != "Darwin",
        reason="sandbox-exec is macOS-only",
    )
    def test_reference_runner_denies_network_command(self):
        import asyncio

        runner = SandboxExecRunner(timeout_s=20.0)
        out = asyncio.run(runner.run_sandboxed("shell_exec", {"command": "true"}))
        assert out == ""
        with pytest.raises(SandboxRunnerUnavailable):
            asyncio.run(
                runner.run_sandboxed(
                    "shell_exec", {"command": "curl -s -m 2 http://127.0.0.1:1 >/dev/null"}
                )
            )

    def test_reference_runner_missing_exec_is_fail_closed(self, tmp_path, monkeypatch):
        import asyncio

        runner = SandboxExecRunner()
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(SandboxRunnerUnavailable):
            asyncio.run(runner.run_sandboxed("shell_exec", {"command": "true"}))
