"""MCP stdio client (plan P1-1).

Speaks the MCP stdio transport (JSON-RPC 2.0, line-delimited) with a spawned
server subprocess: initialize handshake -> tools/list -> tools/call. Deny-first
is NOT implemented here by design — permission evaluation happens in the
funnel layer before call_tool is ever reached; this module is pure transport.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fusion_harness.provider import wire

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPClientError(Exception):
    pass


class MCPStdioClient:
    def __init__(
        self,
        command: List[str],
        server_name: str,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        request_timeout: float = 15.0,
    ) -> None:
        self.command = list(command)
        self.server_name = server_name
        self.env = env
        self.cwd = cwd
        self.request_timeout = request_timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[Any, asyncio.Future] = {}
        self._msg_id = 0
        self._initialized = False

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        asyncio.ensure_future(self._read_loop())
        reply = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fusion-harness", "version": "1.0"},
            },
        )
        if "protocolVersion" not in reply:
            await self.stop()
            raise MCPClientError("server initialize rejected: %s" % reply)
        self._notify("notifications/initialized", {})
        self._initialized = True
        logger.info("mcp server started name=%s pid=%s", self.server_name, self._proc.pid)

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass
        await self._proc.wait()
        self._proc = None
        self._initialized = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPClientError("client stopped"))
        self._pending.clear()
        logger.info("mcp server stopped name=%s", self.server_name)

    async def list_tools(self) -> List["MCPTool"]:
        self._require_initialized()
        reply = await self._request("tools/list", {})
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
        logger.info("mcp tools listed server=%s count=%d", self.server_name, len(tools))
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._require_initialized()
        reply = await self._request("tools/call", {"name": name, "arguments": arguments})
        if reply.get("isError"):
            logger.warning("mcp tool error server=%s tool=%s", self.server_name, name)
        return reply

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MCPClientError("client not initialized (call start() first)")

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                logger.warning("mcp server stdout closed name=%s", self.server_name)
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                kind, msg = wire.decode_line(line)
            except wire.WireError:
                logger.error("mcp wire parse error server=%s", self.server_name)
                continue
            if kind == "response":
                future = self._pending.pop(msg.get("id"), None)
                if future is not None and not future.done():
                    if "error" in msg:
                        err = msg["error"]
                        future.set_exception(
                            MCPClientError(
                                "server error %s: %s" % (err.get("code"), err.get("message"))
                            )
                        )
                    else:
                        future.set_result(msg.get("result") or {})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send(wire.encode_notification(method, params))

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None:
            raise MCPClientError("client not started")
        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        self._send(wire.encode_request(msg_id, method, params))
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            logger.error("mcp request timeout method=%s server=%s", method, self.server_name)
            raise

    def _send(self, text: str) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            raise MCPClientError("mcp server stdin closed")
        self._proc.stdin.write((text + "\n").encode("utf-8"))


class MCPTool:
    def __init__(
        self, name: str, description: str, input_schema: Dict[str, Any], server: str
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.server = server

    @property
    def funnel_name(self) -> str:
        """Canonical tool name inside the funnel: mcp__<server>__<tool>."""
        return "mcp__%s__%s" % (self.server, self.name)


class MCPToolSource:
    """Registry adapter: exposes one MCP server's tools to the funnel under
    namespaced names. Consumers register source tools in their tool registry;
    permission rules match the mcp__<server>__ names like any other tool."""

    def __init__(self, client: MCPStdioClient) -> None:
        self.client = client

    async def tools(self) -> List[MCPTool]:
        return await self.client.list_tools()

    async def invoke(self, tool: MCPTool, arguments: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("mcp tool invoke server=%s tool=%s", tool.server, tool.name)
        return await self.client.call_tool(tool.name, arguments)
