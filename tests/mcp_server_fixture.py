"""Minimal MCP stdio server fixture for transport tests (plan P1-1).

Speaks JSON-RPC 2.0 line protocol: initialize, tools/list (echo + secret_tool),
tools/call. Sync loop — no asyncio needed.
"""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "echo back text", "inputSchema": {"type": "object"}},
    {"name": "secret_tool", "description": "restricted tool", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture"},
                    },
                }
            )
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            if name == "echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "echo:" + json.dumps(params.get("arguments") or {}),
                                }
                            ],
                            "isError": False,
                        },
                    }
                )
            else:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [], "isError": True}})
        elif msg_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )


if __name__ == "__main__":
    main()
