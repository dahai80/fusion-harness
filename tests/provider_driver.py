"""Echo adapter driver for provider-seam tests (#15).

Speaks the ACP-compatible wire protocol over stdio. Emits streaming update
notifications for prompts, round-trips a permission request when the prompt
starts with "!perm". Sync loop — no asyncio needed in the driver.
"""

import json
import sys


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
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"ok": True, "adapter": "echo"}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}})
        elif method == "session/prompt":
            session_id = params.get("session_id", "")
            text = params.get("text", "")
            for part in (text[:4], text[4:8], text[8:]):
                if part:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "session_id": session_id,
                                "update": {"type": "message_chunk", "text": part},
                            },
                        }
                    )
            if text.startswith("!perm"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": "perm-driver-1",
                        "method": "session/request_permission",
                        "params": {
                            "session_id": session_id,
                            "tool": "bash",
                            "request_id": "perm-driver-1",
                        },
                    }
                )
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if reply.get("id") == "perm-driver-1":
                        allowed = bool((reply.get("result") or {}).get("allow"))
                        send(
                            {
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "result": {
                                    "ok": True,
                                    "permission": "granted" if allowed else "denied",
                                },
                            }
                        )
                        break
            else:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"ok": True, "echo": text}})
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )


if __name__ == "__main__":
    main()
