"""MCP Streamable-HTTP transport + orchestrator primitive tests."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fusion_harness.mcp import MCPStreamHttpClient
from fusion_harness.orchestrator import BackgroundPool, ParallelTeam
from fusion_harness.subagent import SubagentRunner, SubagentSpec


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(length))
        method = msg.get("method")
        if method == "initialize":
            self._reply(
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05"}},
                {"Mcp-Session-Id": "sess-42"},
            )
        elif method == "tools/list":
            self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"tools": [{"name": "echo", "description": "", "inputSchema": {}}]},
                }
            )
        elif method == "tools/call":
            self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "isError": False,
                        "content": [
                            {"type": "text", "text": "hi " + msg["params"]["arguments"]["text"]}
                        ],
                    },
                }
            )
        else:
            self._reply({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})


@pytest.fixture()
def http_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d/" % server.server_address[1]
    server.shutdown()
    server.server_close()


class TestMCPHttp:
    def test_roundtrip_with_session(self, http_url):
        async def run():
            client = MCPStreamHttpClient(http_url, server_name="fixture")
            await client.start()
            assert client.session_id == "sess-42"
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "http"})
            await client.stop()
            return tools, result

        tools, result = asyncio.run(run())
        assert [t.funnel_name for t in tools] == ["mcp__fixture__echo"]
        assert result["content"][0]["text"] == "hi http"

    def test_uninitialized_raises(self, http_url):
        client = MCPStreamHttpClient(http_url, server_name="fixture")
        with pytest.raises(Exception):
            asyncio.run(client.list_tools())

    def test_unreachable_fails_loud(self):
        async def run():
            client = MCPStreamHttpClient("http://127.0.0.1:1/", server_name="x")
            await client.start()

        with pytest.raises(Exception):
            asyncio.run(run())


class TestOrchestrator:
    def test_background_pool_collect(self):
        runner = SubagentRunner()
        pool = BackgroundPool(runner)

        async def agent(spec, session):
            session.record_turn(tokens=1)
            return "done-%s" % spec.name

        async def run():
            pool.spawn(SubagentSpec(name="a", prompt="p"), agent)
            pool.spawn(SubagentSpec(name="b", prompt="p"), agent)
            return await pool.collect()

        results = asyncio.run(run())
        assert sorted(r.name for r in results) == ["a", "b"]
        assert all(r.status == "completed" for r in results)

    def test_team_member_failure_does_not_cancel_siblings(self):
        runner = SubagentRunner()
        team = ParallelTeam(runner)

        async def agent(spec, session):
            if spec.name == "boom":
                raise RuntimeError("child exploded")
            return "ok"

        async def run():
            return await team.run(
                [SubagentSpec(name="boom", prompt="p"), SubagentSpec(name="safe", prompt="p")],
                agent,
            )

        results = asyncio.run(run())
        by_name = {r.name: r for r in results}
        assert by_name["boom"].status == "failed"
        assert "child exploded" in by_name["boom"].error
        assert by_name["safe"].status == "completed"

    def test_team_empty_specs(self):
        team = ParallelTeam(SubagentRunner())
        assert asyncio.run(team.run([], None)) == []

    def test_team_summary_only_boundary(self):
        bus_events = []
        from fusion_harness.funnels import LifecycleBus

        bus = LifecycleBus()
        bus.subscribe(lambda ev: bus_events.append(ev.event))
        team = ParallelTeam(SubagentRunner(bus=bus))

        async def agent(spec, session):
            return "summary"

        asyncio.run(team.run([SubagentSpec(name="s", prompt="p")], agent))
        assert set(bus_events) <= {"subagent_start", "subagent_end"}
