"""Phase 3 surpass-bet tests: sandbox tiers (P3-3), cache advisor (P3-1),
memory binding (P3-2), plus the P2-4 eval gate guarding them."""

import pytest

from fusion_harness.funnels import ToolFunnel
from fusion_harness.memory_link import MemoryBridge
from fusion_harness.permissions import PermissionEngine
from fusion_harness.sandbox import ExecutionTier, SandboxPolicy, map_risk_to_tier


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def run_sandboxed(self, tool_name, args):
        self.calls.append((tool_name, args))
        return "sandboxed-result"


class TestSandboxTiers:
    def test_risk_mapping_table(self):
        assert map_risk_to_tier(0.9, "destructive") is ExecutionTier.SANDBOX
        assert map_risk_to_tier(0.6, "network") is ExecutionTier.PROCESS
        assert map_risk_to_tier(0.99, "") is ExecutionTier.SANDBOX
        assert map_risk_to_tier(0.1, "") is ExecutionTier.PROCESS

    def test_sandbox_without_runner_blocks_fail_closed(self):
        policy = SandboxPolicy(runner=None)
        decision = policy.decide("rm_rf", {}, (0.95, "destructive"))
        assert decision.tier is ExecutionTier.BLOCKED
        assert "no runner" in decision.reason

    def test_sandbox_with_runner_routes(self):
        policy = SandboxPolicy(runner=FakeRunner())
        decision = policy.decide("rm_rf", {}, (0.95, "destructive"))
        assert decision.tier is ExecutionTier.SANDBOX

    def test_explicit_blocked_tier(self):
        policy = SandboxPolicy(runner=FakeRunner(), tool_tiers={"danger": ExecutionTier.BLOCKED})
        decision = policy.decide("danger", {}, (0.0, "read"))
        assert decision.tier is ExecutionTier.BLOCKED

    @pytest.mark.asyncio
    async def test_funnel_routes_destructive_via_runner(self):
        runner = FakeRunner()
        funnel = ToolFunnel(
            permissions=PermissionEngine(mode="bypass"),
            sandbox_policy=SandboxPolicy(runner=runner),
        )
        result, is_error = await funnel.run_tool(
            None, "bash", {"cmd": "rm -rf /data"}, lambda a: _ok(a)
        )
        assert is_error is False
        assert result == "sandboxed-result"
        assert runner.calls and runner.calls[0][0] == "bash"

    @pytest.mark.asyncio
    async def test_funnel_denies_sandbox_tier_without_runner(self):
        funnel = ToolFunnel(
            permissions=PermissionEngine(mode="bypass"),
            sandbox_policy=SandboxPolicy(runner=None),
        )
        result, is_error = await funnel.run_tool(
            None, "bash", {"cmd": "rm -rf /data"}, lambda a: _ok(a)
        )
        assert is_error is True
        assert "blocked" in result

    @pytest.mark.asyncio
    async def test_funnel_passthrough_without_policy(self):
        funnel = ToolFunnel(permissions=PermissionEngine(mode="bypass"))
        result, is_error = await funnel.run_tool(
            None, "read_file", {"path": "/x"}, lambda a: _ok(a)
        )
        assert is_error is False and result == "ok"


async def _ok(args):
    return "ok"


class Advisor:
    def __init__(self, keep):
        self.keep = keep
        self.asked = 0

    def suggest_keep_rounds(self, messages):
        self.asked += 1
        return self.keep


class TestCacheAdvisor:
    def test_advisor_raises_keep_rounds(self):
        from fusion_harness.compactor import CompactionConfig, Compactor

        advisor = Advisor(keep=2)
        compactor = Compactor(
            CompactionConfig(context_window=300, keep_recent_rounds=1, cache_advisor=advisor)
        )

        def rnd(t):
            return [
                {"role": "user", "content": t},
                {"role": "assistant", "content": "done " + t},
            ]

        messages = rnd("g1") + rnd("r2") + rnd("r3") + rnd("r4")
        out = compactor.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        assert advisor.asked == 1
        kept_users = [m for m in out if m.get("role") == "user"]
        assert len(kept_users) >= 2, "advisor keep=2 must keep 2 recent rounds"

    def test_advisor_never_shrinks_below_config(self):
        from fusion_harness.compactor import CompactionConfig, Compactor

        compactor = Compactor(CompactionConfig(keep_recent_rounds=3, cache_advisor=Advisor(keep=1)))
        assert compactor.config.keep_recent_rounds == 3

    def test_advisor_failure_falls_back(self):
        from fusion_harness.compactor import CompactionConfig, Compactor

        class Bad:
            def suggest_keep_rounds(self, messages):
                raise RuntimeError("advisor down")

        compactor = Compactor(
            CompactionConfig(context_window=300, keep_recent_rounds=1, cache_advisor=Bad())
        )

        def rnd(t):
            return [{"role": "user", "content": t}, {"role": "assistant", "content": "x " + t}]

        out = compactor.compact(
            rnd("a") + rnd("b") + rnd("c"), level="warning", summarize=lambda m, hard=False: "[s]"
        )
        assert out  # completed without raising


class FakeSink:
    def __init__(self):
        self.items = {}

    def store(self, key, payload, metadata=None):
        self.items[key] = payload


class FakeSource:
    def __init__(self, items):
        self.items = items

    def retrieve(self, query, limit=5):
        return [i for i in self.items if query.lower() in str(i.get("payload", "")).lower()][:limit]


class TestMemoryBinding:
    def test_persist_and_recall_roundtrip(self):
        sink = FakeSink()
        bridge = MemoryBridge(
            sink=sink, source=FakeSource([{"payload": "settlement uses banker rounding"}])
        )
        assert bridge.persist_compaction("settlement uses banker rounding", session_id="s1") is True
        assert sink.items["compaction:s1"] == "settlement uses banker rounding"
        recalled = bridge.recall("banker rounding")
        assert len(recalled) == 1

    def test_no_sink_is_noop(self):
        bridge = MemoryBridge()
        assert bridge.persist_compaction("x") is False
        assert bridge.recall("anything") == []

    def test_sink_failure_swallowed_visible(self):
        class Boom:
            def store(self, key, payload, metadata=None):
                raise RuntimeError("down")

        bridge = MemoryBridge(sink=Boom())
        assert bridge.persist_compaction("x") is False

    def test_inject_context_just_in_time(self):
        bridge = MemoryBridge(source=FakeSource([{"payload": "fact one"}, {"payload": "fact two"}]))
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "continue"}]
        out = bridge.inject_context(messages, "fact")
        assert out[1]["role"] == "system"
        assert "[Recalled memory]" in out[1]["content"]
        assert "fact one" in out[1]["content"]
        assert out[0]["content"] == "rules" and out[2]["role"] == "user"

    def test_inject_noop_when_no_match(self):
        bridge = MemoryBridge(source=FakeSource([]))
        messages = [{"role": "user", "content": "hi"}]
        assert bridge.inject_context(messages, "nothing") is messages


class TestEvalGateGuardsPhase3:
    """Minimalism gate: the P2-4 baseline must stay green after Phase 3
    compactor changes (cache advisor path)."""

    def test_baseline_holds_with_advisor(self):
        import json
        import os
        import sys

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from bench.compaction_eval import check_regression, load_baseline, run_eval

        from fusion_harness.compactor import CompactionConfig, Compactor

        bench_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"
        )
        trajectory = json.load(
            open(os.path.join(bench_dir, "trajectories", "compaction_future_questions.json"))
        )
        compactor = Compactor(CompactionConfig(keep_recent_rounds=1, cache_advisor=Advisor(keep=1)))
        result = run_eval(trajectory, compactor)
        verdict = check_regression(result, load_baseline(os.path.join(bench_dir, "baseline.json")))
        assert verdict["ok"], result["missed"]
