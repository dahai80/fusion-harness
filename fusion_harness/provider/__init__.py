"""Provider seam (#15) — one adapter per agent runtime, host runtime-agnostic.

Pure stdlib. Consumers register adapters on AdapterHost; SubprocessSessionRuntime
drives a CLI adapter over the ACP-compatible wire. Transport, not policy:
budget funnels / permission engine / compaction apply upstream, unchanged.
"""

from fusion_harness.provider.adapter import AdapterHost, PermissionHandler, ProviderAdapter
from fusion_harness.provider.events import (
    ERROR,
    EVENT_TYPES,
    MESSAGE_CHUNK,
    PERMISSION_REQUEST,
    PERMISSION_RESPONSE,
    SESSION_NEW,
    STATE_CHANGED,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
    SessionEvent,
)
from fusion_harness.provider.session import RuntimeNotReady, SessionState, SubprocessSessionRuntime
from fusion_harness.provider.wire import (
    METHOD_INITIALIZE,
    METHOD_PERMISSION_REQUEST,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    METHOD_SESSION_UPDATE,
    PARSE_ERROR,
)

WIRE_METHODS = (
    METHOD_INITIALIZE,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    METHOD_SESSION_UPDATE,
    METHOD_PERMISSION_REQUEST,
)

__all__ = [
    "AdapterHost",
    "PermissionHandler",
    "ProviderAdapter",
    "EventBus",
    "SessionEvent",
    "SessionState",
    "SubprocessSessionRuntime",
    "RuntimeNotReady",
    "WIRE_METHODS",
    "ERROR",
    "EVENT_TYPES",
    "MESSAGE_CHUNK",
    "PERMISSION_REQUEST",
    "PERMISSION_RESPONSE",
    "SESSION_NEW",
    "STATE_CHANGED",
    "TOOL_CALL",
    "TOOL_RESULT",
    "METHOD_INITIALIZE",
    "METHOD_PERMISSION_REQUEST",
    "METHOD_SESSION_NEW",
    "METHOD_SESSION_PROMPT",
    "METHOD_SESSION_UPDATE",
    "PARSE_ERROR",
]
