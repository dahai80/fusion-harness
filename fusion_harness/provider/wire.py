"""ACP-compatible wire layer — JSON-RPC 2.0, line-delimited over stdio (#15).

Subset of the Agent Client Protocol (as used by t3code provider-acp):
initialize / session-new / session-prompt requests, streaming session-update
notifications, session-request_permission round-trip. Stdlib json only; the
transport stays swappable (gateway/websocket later without touching adapters).
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_SESSION_UPDATE = "session/update"
METHOD_PERMISSION_REQUEST = "session/request_permission"
METHOD_PERMISSION_RESPONSE = "session/permission_response"


class WireError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601


def encode_request(msg_id: int, method: str, params: Optional[Dict[str, Any]] = None) -> str:
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


def encode_response(msg_id: int, result: Any = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False)


def encode_error(msg_id: int, code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )


def encode_notification(method: str, params: Dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False)


def decode_line(line: str) -> Tuple[str, Dict[str, Any]]:
    """Parse one wire line -> ("request"|"response"|"notification"|"error", message).

    Never raises on malformed JSON beyond WireError(PARSE_ERROR); callers turn
    that into an encode_error reply per JSON-RPC spec.
    """
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WireError(PARSE_ERROR, "invalid json: %s" % exc) from None
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        raise WireError(PARSE_ERROR, "not a jsonrpc 2.0 message")
    if "method" in msg:
        kind = "request" if "id" in msg else "notification"
    elif "error" in msg:
        kind = "error"
    else:
        kind = "response"
    return kind, msg
