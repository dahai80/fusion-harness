"""MCP client transports (plan P1-1).

MCP tools enter the harness tool funnel like native tools — same permission
engine, same budget accounting, no new privilege surface. Stdlib only:
JSON-RPC over stdio (line-delimited, reusing the provider wire codec) and
over Streamable-HTTP.
"""

from fusion_harness.mcp.client import MCPStdioClient, MCPTool, MCPToolSource
from fusion_harness.mcp.http_client import MCPStreamHttpClient

__all__ = ["MCPStdioClient", "MCPStreamHttpClient", "MCPTool", "MCPToolSource"]
