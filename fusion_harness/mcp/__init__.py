"""MCP client transport (plan P1-1).

MCP tools enter the harness tool funnel like native tools — same permission
engine, same budget accounting, no new privilege surface. Stdlib only:
JSON-RPC over stdio, reusing the provider wire codec.
"""

from fusion_harness.mcp.client import MCPStdioClient, MCPTool, MCPToolSource

__all__ = ["MCPStdioClient", "MCPTool", "MCPToolSource"]
