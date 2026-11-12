"""Acceptance verification tests (evolution plan 2026-11, kernel-scope gaps).

Covers the five gaps found auditing delivered code against the plan doc:
1. MCP tool deny-path through ToolFunnel (P1-1)
2. Compaction threshold switches on token-counter identity (P1-2)
3. Survival contract across every compaction path incl. MLX gateway (P1-3)
4. Subagent budget via TokenLedger + child PermissionEngine narrowing (P2-1)
5. Crash-resilient checkpoint reopen + ReceiptCache replay no-op (P2-2)
"""

import asyncio
import os
import signal
import subprocess
import sys
import tempfile

import pytest

from fusion_harness.checkpoint import CheckpointStore
from fusion_harness.compactor import CompactionConfig, Compactor
from fusion_harness.funnels import ToolFunnel
from fusion_harness.mcp import MCPStdioClient, MCPToolSource
from fusion_harness.permissions import PermissionEngine, PermissionRule
from fusion_harness.policies import ReceiptCache, TokenLedger
from fusion_harness.subagent import SubagentRunner, SubagentSpec
from fusion_harness.tokens import CJKAwareHeuristicTokenCounter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_FIXTURE = os.path.join(REPO_ROOT, "tests", "mcp_server_fixture.py")


class TestMCPDenyPath:
    def _client(self):
        return MCPStdioClient(
            [sys.executable, MCP_FIXTURE], server_name="fixture", request_timeout=15.0
        )

    def test_deny_rule_blocks_mcp_tool_before_invoke(self):
        async def run():
            client = self._client()
            await client.start()
            source = MCPToolSource(client)
            engine = PermissionEngine(
                rules=[
                    PermissionRule(
                        action="deny",
                        tool=r"^mcp__fixture__secret_tool$",
                        layer="global",
                        description="secret tool denied",
                    )
                ],
                mode="default",
            )
            funnel = ToolFunnel(permissions=engine)
            calls = []

            async def executor(args):
                calls.append(args)
                tool = (await source.tools())[1]
                return str(await source.invoke(tool, args))

            result, is_error = await funnel.run_tool(
                ctx=None,
                tool_name="mcp__fixture__secret_tool",
                args={},
                executor=executor,
            )
            await client.stop()
            return result, is_error, calls

        result, is_error, calls = asyncio.run(run())
        assert is_error is True
        assert "permission" in result.lower() or "blocked" in result.lower()
        assert calls == []

    def test_allow_path_reaches_mcp_client(self):
        async def run():
            client = self._client()
            await client.start()
            source = MCPToolSource(client)
            engine = PermissionEngine(
                rules=[
                    PermissionRule(
                        action="allow",
                        tool=r"^mcp__fixture__echo$",
                        layer="global",
                    )
                ],
                mode="default",
            )
            funnel = ToolFunnel(permissions=engine)

            async def executor(args):
                tool = next(t for t in await source.tools() if t.name == "echo")
                res = await source.invoke(tool, args)
                return res["content"][0]["text"]

            result, is_error = await funnel.run_tool(
                ctx=None, tool_name="mcp__fixture__echo", args={"text": "ping"}, executor=executor
            )
            await client.stop()
            return result, is_error

        result, is_error = asyncio.run(run())
        assert is_error is False
        assert "ping" in result


class TestCounterIdentityThresholds:
    def test_cjk_counter_compacts_where_heuristic_does_not(self):
        cjk_text = "这是一段很长的中文任务描述" * 80
        messages = [{"role": "user", "content": cjk_text}]
        cfg = CompactionConfig(
            context_window=1000, warning_buffer=200, error_buffer=100, manual_buffer=50
        )

        heuristic = Compactor(cfg)
        cjk = Compactor(
            CompactionConfig(
                context_window=1000,
                warning_buffer=200,
                error_buffer=100,
                manual_buffer=50,
                token_counter=CJKAwareHeuristicTokenCounter(),
            )
        )
        assert heuristic.should_compact(messages) == "none"
        assert cjk.should_compact(messages) in ("warning", "error", "manual")


class TestSurvivalContractMatrix:
    def _messages(self):
        return [
            {"role": "system", "content": "PINNED-SYSTEM-CONTRACT-42"},
            {"role": "user", "content": "GOAL-rides-the-compaction"},
        ] + [
            m
            for i in range(12)
            for m in (
                {"role": "user", "content": "round %d payload" % i},
                {"role": "tool", "content": "x" * 500, "tool_call_id": "t%d" % i},
            )
        ]

    def _assert_survives(self, out):
        contents = [str(m.get("content") or "") for m in out]
        assert "PINNED-SYSTEM-CONTRACT-42" in contents
        assert any("GOAL-rides-the-compaction" in c for c in contents)

    @pytest.mark.parametrize("level", ["warning", "error", "manual"])
    def test_survival_all_levels(self, level):
        cfg = CompactionConfig(context_window=500, warning_buffer=100, error_buffer=50)
        compactor = Compactor(cfg)
        out = compactor.compact(self._messages(), level=level)
        self._assert_survives(out)

    def test_survival_reactive_strip(self):
        compactor = Compactor(CompactionConfig())
        out = compactor.reactive_strip(self._messages())
        self._assert_survives(out)

    def test_survival_mlx_gateway_path(self):
        class FakeGateway:
            async def chat(self, **kwargs):
                class Resp:
                    content = "gateway summary of prior context"

                return Resp()

        cfg = CompactionConfig(context_window=500, warning_buffer=100)
        compactor = Compactor(cfg, llm_gateway=FakeGateway())

        async def run():
            return await compactor.compact_async(self._messages(), level="warning")

        out = asyncio.run(run())
        self._assert_survives(out)


class TestSubagentLedgerAndChildEngine:
    def test_budget_enforced_via_ledger_balance(self):
        runner = SubagentRunner(ledger_factory=lambda: TokenLedger())

        async def agent(spec, session):
            while session.record_turn(tokens=600) == "ok":
                pass
            return "done"

        result = asyncio.run(
            runner.run(SubagentSpec(name="capped", prompt="p", budget_tokens=1000), agent)
        )
        assert result.status == "capped"
        assert result.tokens_spent == 1200

    def test_ledger_receives_scoped_entries(self):
        ledger = TokenLedger()
        runner = SubagentRunner(ledger_factory=lambda: ledger)

        async def agent(spec, session):
            session.record_turn(tokens=100)
            return "ok"

        result = asyncio.run(
            runner.run(SubagentSpec(name="scoped", prompt="p", budget_tokens=5000), agent)
        )
        assert result.status == "completed"
        entries = ledger.entries(scope=result.subagent_id)
        assert len(entries) == 1
        assert entries[0]["completion_tokens"] == 100

    def test_child_engine_deny_wins_over_spec_allow(self):
        parent = PermissionEngine(
            rules=[
                PermissionRule(
                    action="deny", tool=r"^bash$", layer="global", description="no shell"
                )
            ],
            mode="default",
        )
        spec = SubagentSpec(name="kid", prompt="p", tool_allowlist=["bash", "read_file"])
        child = SubagentRunner.child_engine(parent, spec)
        assert child.evaluate("bash", {}).decision == "deny"
        assert child.evaluate("read_file", {}).decision == "allow"
        assert child.evaluate("unknown_tool", {}).decision != "allow"

    def test_child_engine_mode_never_widens(self):
        parent = PermissionEngine(rules=[], mode="plan")
        spec = SubagentSpec(name="kid", prompt="p", permission_mode="bypass")
        child = SubagentRunner.child_engine(parent, spec)
        assert getattr(child.mode, "value", child.mode) == "plan"


class TestCrashResilience:
    def test_checkpoint_survives_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "ckpt.sqlite")
            child_code = (
                "import os, signal, sys\n"
                "sys.path.insert(0, %r)\n"
                "from fusion_harness.checkpoint import CheckpointState, CheckpointStore\n"
                "store = CheckpointStore(%r)\n"
                "store.save('s-crash', CheckpointState("
                "messages=[{'role': 'user', 'content': 'pre-kill-state'}], "
                "budget_spent_tokens=321, permission_mode='approved', outbox_offset=9))\n"
                "os.kill(os.getpid(), signal.SIGKILL)\n" % (REPO_ROOT, db)
            )
            proc = subprocess.run(
                [sys.executable, "-c", child_code],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": REPO_ROOT},
                capture_output=True,
                timeout=60,
            )
            assert proc.returncode == -signal.SIGKILL
            reopened = CheckpointStore(db)
            state = reopened.latest("s-crash")
            assert state is not None
            assert state.budget_spent_tokens == 321
            assert state.outbox_offset == 9
            assert state.messages[0]["content"] == "pre-kill-state"
            assert state.permission_mode == "approved"
            reopened.close()

    def test_receipt_cache_replay_noop(self):
        cache = ReceiptCache(":memory:")
        calls = []

        def side_effect():
            calls.append(1)
            return "committed-result"

        first, replayed_first = cache.first("cmd-1", "agg", side_effect)
        second, replayed_second = cache.first("cmd-1", "agg", side_effect)
        assert replayed_first is False
        assert replayed_second is True
        assert first == second == "committed-result"
        assert len(calls) == 1
        cache.close()
