"""Security-intel client tests (issue #24, fusion-security contract v1).

Fake HTTP server stands in for fusion-security /api/v1/agent/risk-score
and /api/v1/agent/decisions; verifies request shape, advisory-by-default
hints, enforce-hints denial, offline no-op, and decision push.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fusion_harness.funnels import ToolFunnel
from fusion_harness.permissions import PermissionEngine, PermissionRule
from fusion_harness.security_intel import RiskIntelClient


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-security"

    def log_message(self, *args):
        pass

    def _reply(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, msg))
        if self.path == "/api/v1/agent/risk-score":
            if msg["tool_name"] == "danger_tool":
                self._reply(
                    {
                        "schema_version": 1,
                        "tool_name": "danger_tool",
                        "score": 95,
                        "risk_class": "critical",
                        "policy_hints": ["deny"],
                        "signals": ["destructive pattern"],
                    }
                )
            else:
                self._reply(
                    {
                        "schema_version": 1,
                        "tool_name": msg["tool_name"],
                        "score": 10,
                        "risk_class": "low",
                        "policy_hints": ["allow"],
                        "signals": [],
                    }
                )
        elif self.path == "/api/v1/agent/decisions":
            self._reply({"accepted": True})
        else:
            self._reply({"error": "not found"}, status=404)


@pytest.fixture()
def security_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _client(server, enforce=False):
    return RiskIntelClient(
        "http://127.0.0.1:%d" % server.server_address[1],
        tenant_id="t1",
        caller={"agent_id": "a1"},
        enforce_hints=enforce,
    )


def _engine():
    return PermissionEngine(
        rules=[
            PermissionRule(action="allow", tool=r"^safe_tool$", layer="global"),
            PermissionRule(action="allow", tool=r"^danger_tool$", layer="global"),
            PermissionRule(action="deny", tool=r"^unknown_tool$", layer="global"),
        ],
        mode="default",
    )


class TestRiskIntelClient:
    def test_fetch_risk_parses_contract(self, security_server):
        intel = asyncio.run(_client(security_server).fetch_risk("danger_tool", {}))
        assert intel.score == 95
        assert intel.risk_class == "critical"
        assert intel.policy_hints == ["deny"]
        path, body = security_server.requests[0]
        assert path == "/api/v1/agent/risk-score"
        assert body["tenant_id"] == "t1"
        assert body["caller"] == {"agent_id": "a1"}

    def test_offline_returns_none_noop(self):
        client = RiskIntelClient("http://127.0.0.1:1/", request_timeout=1.0)
        assert asyncio.run(client.fetch_risk("any", {})) is None
        assert asyncio.run(client.push_decision("any", "allow")) is False

    def test_push_decision_roundtrip_and_validation(self, security_server):
        client = _client(security_server)
        assert asyncio.run(client.push_decision("safe_tool", "deny", reason="r1")) is True
        path, body = security_server.requests[-1]
        assert path == "/api/v1/agent/decisions"
        assert body["decision"] == "deny"
        assert asyncio.run(client.push_decision("safe_tool", "bogus")) is False


class TestFunnelIntelWiring:
    def test_advisory_hint_no_behavior_change(self, security_server):
        funnel = ToolFunnel(permissions=_engine(), security_intel=_client(security_server))

        async def executor(args):
            return "ran"

        result, is_error = asyncio.run(funnel.run_tool(None, "safe_tool", {}, executor))
        assert is_error is False
        assert result == "ran"

    def test_enforced_deny_hint_blocks_execution(self, security_server):
        calls = []
        funnel = ToolFunnel(
            permissions=_engine(), security_intel=_client(security_server, enforce=True)
        )

        async def executor(args):
            calls.append(1)
            return "ran"

        result, is_error = asyncio.run(funnel.run_tool(None, "danger_tool", {}, executor))
        assert is_error is True
        assert "security intel" in result
        assert calls == []
        decisions = [m for p, m in security_server.requests if "decision" in m]
        assert decisions and decisions[-1]["decision"] == "deny"

    def test_permission_deny_pushes_decision(self, security_server):
        funnel = ToolFunnel(permissions=_engine(), security_intel=_client(security_server))

        async def executor(args):
            return "ran"

        result, is_error = asyncio.run(funnel.run_tool(None, "unknown_tool", {}, executor))
        assert is_error is True
        decisions = [m for p, m in security_server.requests if "decision" in m]
        assert decisions and decisions[-1]["decision"] == "deny"

    def test_no_intel_client_no_change(self):
        funnel = ToolFunnel(permissions=_engine())

        async def executor(args):
            return "ran"

        result, is_error = asyncio.run(funnel.run_tool(None, "safe_tool", {}, executor))
        assert is_error is False and result == "ran"
