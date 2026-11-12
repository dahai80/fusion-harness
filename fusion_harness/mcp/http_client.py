"""MCP Streamable-HTTP client (plan P1-1, HTTP transport half).

Same protocol surface as MCPStdioClient (initialize -> tools/list ->
tools/call) but over HTTP POST per the MCP Streamable-HTTP transport:
JSON-RPC request body, JSON or SSE response, optional Mcp-Session-Id.
Pure stdlib (urllib.request off-thread); deny-first stays in the funnel
layer. Reuses MCPTool / MCPToolSource from the stdio module.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from fusion_harness.mcp.client import MCPClientError, MCP_PROTOCOL_VERSION
from fusion_harness.mcp.client import MCPTool

logger = logging.getLogger(__name__)


class MCPStreamHttpClient:
    def __init__(
        self,
        url: str,
        server_name: str,
        request_timeout: float = 15.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.url = url
        self.server_name = server_name
        self.request_timeout = request_timeout
        self.headers = dict(headers or {})
        self.session_id: Optional[str] = None
        self._initialized = False
        self._msg_id = 0

    async def start(self) -> None:
        if self._initialized:
            return
        reply, resp_headers = await self._post(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fusion-harness", "version": "1.0"},
            },
            expect_response=True,
        )
        if "protocolVersion" not in reply:
            raise MCPClientError("server initialize rejected: %s" % reply)
        self.session_id = resp_headers.get("Mcp-Session-Id") or resp_headers.get("mcp-session-id")
        await self._notify("notifications/initialized", {})
        self._initialized = True
        logger.info("mcp http server started name=%s url=%s", self.server_name, self.url)

    async def stop(self) -> None:
        if not self._initialized:
            return
        try:
            await self._post("shutdown", {}, expect_response=False)
        except (MCPClientError, OSError) as exc:
            logger.info("mcp http shutdown skipped server=%s err=%s", self.server_name, exc)
        self._initialized = False
        logger.info("mcp http server stopped name=%s", self.server_name)

    async def list_tools(self) -> List[MCPTool]:
        self._require_initialized()
        reply, _ = await self._post("tools/list", {}, expect_response=True)
        tools = []
        for raw in reply.get("tools") or []:
            tools.append(
                MCPTool(
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or {},
                    server=self.server_name,
                )
            )
        logger.info("mcp http tools listed server=%s count=%d", self.server_name, len(tools))
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._require_initialized()
        reply, _ = await self._post(
            "tools/call", {"name": name, "arguments": arguments}, expect_response=True
        )
        if reply.get("isError"):
            logger.warning("mcp http tool error server=%s tool=%s", self.server_name, name)
        return reply

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MCPClientError("client not initialized (call start() first)")

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._post(method, params, expect_response=False, notification=True)

    async def _post(
        self, method: str, params: Dict[str, Any], expect_response: bool, notification: bool = False
    ) -> tuple:
        self._msg_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self._msg_id
        return await asyncio.to_thread(self._post_blocking, payload, expect_response)

    def _post_blocking(self, payload: Dict[str, Any], expect_response: bool) -> tuple:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        for key, value in self.headers.items():
            req.add_header(key, value)
        if self.session_id:
            req.add_header("Mcp-Session-Id", self.session_id)
        try:
            resp = urllib.request.urlopen(req, timeout=self.request_timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            logger.error("mcp http error server=%s status=%s", self.server_name, exc.code)
            raise MCPClientError("http %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            logger.error("mcp http unreachable server=%s err=%s", self.server_name, exc)
            raise MCPClientError("http unreachable: %s" % exc) from exc
        with resp:
            resp_headers = {k: v for k, v in resp.headers.items()}
            if resp.status == 202 or not expect_response:
                return {}, resp_headers
            raw = resp.read().decode("utf-8", errors="replace")
        content_type = resp_headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            result, error = self._parse_sse(raw)
        else:
            msg = json.loads(raw)
            result, error = msg.get("result") or {}, msg.get("error")
        if error:
            raise MCPClientError("server error %s: %s" % (error.get("code"), error.get("message")))
        return result or {}, resp_headers

    @staticmethod
    def _parse_sse(raw: str) -> tuple:
        result: Dict[str, Any] = {}
        error: Dict[str, Any] | None = None
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if "error" in msg:
                error = msg["error"]
            elif "result" in msg:
                result = msg["result"] or {}
        return result, error


__all__ = ["MCPStreamHttpClient"]
