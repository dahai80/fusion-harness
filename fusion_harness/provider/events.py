"""UI-agnostic streaming event model for provider sessions (#15).

Pattern adopted from t3code provider-acp runtimeModel — re-implemented on
stdlib dataclasses. Events are the only thing crossing the seam; adapters
and transports never touch UI state. Sorted constants keep wire names stable.
"""

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SESSION_NEW = "session/new"
MESSAGE_CHUNK = "session/message_chunk"
TOOL_CALL = "session/tool_call"
TOOL_RESULT = "session/tool_result"
PERMISSION_REQUEST = "session/permission_request"
PERMISSION_RESPONSE = "session/permission_response"
STATE_CHANGED = "session/state_changed"
ERROR = "session/error"

EVENT_TYPES = (
    SESSION_NEW,
    MESSAGE_CHUNK,
    TOOL_CALL,
    TOOL_RESULT,
    PERMISSION_REQUEST,
    PERMISSION_RESPONSE,
    STATE_CHANGED,
    ERROR,
)


@dataclass(frozen=True)
class SessionEvent:
    type: str
    session_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError("unknown event type: %s" % self.type)
        if not self.session_id:
            raise ValueError("session_id is required")

    def with_payload(self, **updates: Any) -> "SessionEvent":
        return replace(self, payload={**self.payload, **updates})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "payload": dict(self.payload),
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionEvent":
        return cls(
            type=str(data["type"]),
            session_id=str(data["session_id"]),
            payload=dict(data.get("payload") or {}),
            request_id=data.get("request_id"),
            timestamp=float(data.get("timestamp") or time.time()),
        )


class EventBus:
    """Fan-out point for session events; subscribers are sync callbacks."""

    def __init__(self) -> None:
        self._subscribers: List[Callable[[SessionEvent], None]] = []

    def subscribe(self, callback: Callable[[SessionEvent], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        logger.debug("event bus subscriber added total=%d", len(self._subscribers))

        def _unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: SessionEvent) -> None:
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                logger.exception("event subscriber failed type=%s", event.type)
