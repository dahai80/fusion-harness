"""Phase 1 convergence tests: token counting (P1-2), tool-result clearing
(P1-4), survival contract (P1-3), MCP transport (P1-1)."""

import asyncio
import os
import sys

import pytest

from fusion_harness.compactor import CompactionConfig, Compactor
from fusion_harness.mcp import MCPStdioClient, MCPToolSource
from fusion_harness.tokens import (
    CJKAwareHeuristicTokenCounter,
    HeuristicTokenCounter,
    count_messages,
)

MCP_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server_fixture.py")


class TestTokenCounting:
    def test_heuristic_matches_legacy_chars_over_4(self):
        assert HeuristicTokenCounter().count_text("abcd") == 1
        assert HeuristicTokenCounter().count_text("a" * 401) == 100

    def test_cjk_counter_counts_cjk_as_one(self):
        counter = CJKAwareHeuristicTokenCounter()
        assert counter.count_text("测" * 100) == 100
        assert counter.count_text("a" * 40) == 10

    def test_cjk_delta_versus_legacy(self):
        text = "测试" * 50
        legacy = HeuristicTokenCounter().count_text(text)
        aware = CJKAwareHeuristicTokenCounter().count_text(text)
        assert aware > legacy

    def test_count_messages_includes_tool_calls(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "x"}'}}],
            },
        ]
        total = count_messages(msgs, HeuristicTokenCounter())
        assert total >= 2

    def test_compactor_uses_injected_counter(self):
        config = CompactionConfig(token_counter=CJKAwareHeuristicTokenCounter())
        compactor = Compactor(config=config)
        messages = [{"role": "user", "content": "测" * 100}]
        assert compactor.estimate_tokens(messages) == 100


def _round(text, tool_out):
    return [
        {"role": "user", "content": text},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": tool_out},
        {"role": "assistant", "content": "done " + text},
    ]


class TestToolResultClearing:
    def test_old_tool_results_cleared_recent_kept(self):
        compactor = Compactor(CompactionConfig(keep_recent_rounds=1))
        messages = (
            [{"role": "system", "content": "sys rules"}]
            + _round("r1", "output-line-1\n" * 30)
            + _round("r2", "output-line-2\n" * 30)
            + _round("r3", "recent output")
        )
        out = compactor.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        cleared = [
            m for m in tool_msgs if str(m.get("content", "")).startswith("[tool result cleared:")
        ]
        assert cleared, "old tool results should be stubbed"
        assert any("recent output" in str(m.get("content")) for m in tool_msgs), "recent round kept"

    def test_stub_preserves_identity_and_bytes(self):
        compactor = Compactor(CompactionConfig(keep_recent_rounds=1, clear_tool_results=True))
        messages = _round("only", "x" * 123) * 3
        out = compactor.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        stubs = [m for m in out if str(m.get("content", "")).startswith("[tool result cleared:")]
        assert stubs and "bytes=123" in stubs[0]["content"]

    def test_clearing_disabled(self):
        compactor = Compactor(CompactionConfig(keep_recent_rounds=1, clear_tool_results=False))
        messages = _round("r1", "out\n" * 40) + _round("r2", "out\n" * 40) + _round("r3", "x")
        out = compactor.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        stubs = [m for m in out if str(m.get("content", "")).startswith("[tool result cleared:")]
        assert not stubs


class TestSurvivalContract:
    def test_system_rules_survive_hard_compact(self):
        compactor = Compactor(CompactionConfig())
        pinned = {"role": "system", "content": "PERMISSION RULE: never read .env"}
        messages = [pinned] + _round("r1", "out\n" * 50) * 6
        out = compactor.compact(messages, level="error", summarize=lambda m, hard=False: "[s]")
        assert any(
            m.get("role") == "system" and "never read .env" in str(m.get("content")) for m in out
        )

    def test_task_goal_re_pinned_when_compacted_away(self):
        compactor = Compactor(CompactionConfig(context_window=300, keep_recent_rounds=1))
        goal = "Refactor the payment module without breaking the API"
        messages = (
            [{"role": "user", "content": goal}]
            + _round("r1", "out\n" * 60)
            + _round("r2", "out\n" * 60)
            + _round("r3", "out\n" * 60)
        )
        out = compactor.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        assert any("[Pinned task goal]" in str(m.get("content")) for m in out)

    def test_goal_not_duplicated_when_still_present(self):
        c = Compactor(CompactionConfig(keep_recent_rounds=1))
        goal = "Build the thing"
        messages = (
            [{"role": "user", "content": goal}]
            + _round("r1", "out\n" * 40)
            + _round("r2", "out\n" * 40)
            + [{"role": "user", "content": goal}]
        )
        out = c.compact(messages, level="warning", summarize=lambda m, hard=False: "[s]")
        pins = [m for m in out if "[Pinned task goal]" in str(m.get("content"))]
        assert not pins

    def test_manifest_introspection(self):
        compactor = Compactor(CompactionConfig())
        manifest = compactor.survival_manifest(
            [{"role": "system", "content": "rules"}, {"role": "user", "content": "do the work"}]
        )
        assert manifest["pinned_system_messages"] == 1
        assert manifest["task_goal_present"] is True


class TestMCPTransport:
    def _client(self):
        return MCPStdioClient(
            [sys.executable, MCP_FIXTURE], server_name="fixture", request_timeout=15.0
        )

    def test_list_and_call_roundtrip(self):
        async def run():
            client = self._client()
            await client.start()
            tools = await client.list_tools()
            names = [t.name for t in tools]
            assert "echo" in names and "secret_tool" in names
            result = await client.call_tool("echo", {"text": "hi"})
            assert result["isError"] is False
            assert "hi" in result["content"][0]["text"]
            await client.stop()
            return tools

        tools = asyncio.run(run())
        source = MCPToolSource(self._client())
        assert source.tools.__name__ == "tools"
        funnel_names = ["mcp__fixture__" + t.name for t in tools]
        assert "mcp__fixture__echo" in funnel_names

    def test_funnel_namespacing(self):
        from fusion_harness.mcp import MCPTool

        tool = MCPTool(name="echo", description="", input_schema={}, server="fixture")
        assert tool.funnel_name == "mcp__fixture__echo"

    def test_error_result_flagged(self):
        async def run():
            client = self._client()
            await client.start()
            result = await client.call_tool("secret_tool", {})
            await client.stop()
            return result

        result = asyncio.run(run())
        assert result["isError"] is True

    def test_uninitialized_raises(self):
        client = self._client()
        with pytest.raises(Exception):
            asyncio.run(client.list_tools())

    def test_stdlib_only_mcp_module(self):
        import ast

        pkg_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fusion_harness", "mcp"
        )
        stdlib_top = set(sys.stdlib_module_names)
        for fname in os.listdir(pkg_dir):
            if not fname.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(pkg_dir, fname)).read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for mod in mods:
                    root = mod.split(".")[0]
                    assert root in stdlib_top or root == "fusion_harness", "%s imports %s" % (
                        fname,
                        mod,
                    )
