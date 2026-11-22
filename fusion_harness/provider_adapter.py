"""Provider adapter contract K1 (issue #158, Paseo parity H1/H7).

Reference shape: Paseo's adapter seam (v0.17.1,
server/src/server/agent/agent-sdk-types.ts) — the working OSS reference
for the K1 kernel abstraction roadmap:

- `AgentClient` / `AgentSession` Protocol boundary: each provider manages
  its own auth and exposes a session-resume handle.
- Normalized `ToolCallDetail` across providers (shell / read / edit /
  write / search / ...).
- Normalized `AgentStreamEvent` variants at the adapter edge.
- `AgentCapabilityFlags` dual-channel gating: clients read capability
  flags instead of provider sniffing (Paseo docs/protocol-compatibility.md:
  "capability flags live in features on server_info", no fallback paths
  inside features).

Harness differentiation stays ours: Paseo has no cross-provider context
compaction — the budget funnel / compaction layer is NOT part of this
seam; adapters never touch it (compaction owns `context_compaction` as a
capability flag only).

Pure stdlib (`typing.Protocol`, dataclasses), deterministic, zero model
calls.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Normalized tool kinds across providers (Paseo ToolCallDetail kind union).
TOOL_KINDS = ("shell", "read", "edit", "write", "search", "web", "mcp", "other")

# Normalized stream-event variants at the adapter edge (Paseo
# AgentStreamEvent ~18-variant union, harness subset + lifecycle events).
EVENT_VARIANTS = (
    "session_started",
    "session_resumed",
    "turn_started",
    "message_started",
    "message_delta",
    "message_completed",
    "text_delta",
    "thinking_delta",
    "tool_call_started",
    "tool_call_delta",
    "tool_call_completed",
    "tool_result",
    "permission_requested",
    "plan_updated",
    "usage_delta",
    "checkpoint",
    "turn_completed",
    "error",
)

__all__ = [
    "EVENT_VARIANTS",
    "TOOL_KINDS",
    "AgentCapabilityFlags",
    "AgentClient",
    "AgentSession",
    "AgentStreamEvent",
    "ToolCallDetail",
    "normalize_tool_kind",
]


class AdapterEventError(ValueError):
    """Unknown stream-event variant or tool kind at the adapter edge."""


def normalize_tool_kind(tool_name: str) -> str:
    """Deterministic provider-tool-name -> normalized kind mapping.
    Unknown names fall through to "other" (never raises)."""
    lowered = (tool_name or "").lower()
    if any(token in lowered for token in ("bash", "shell", "exec", "command", "terminal")):
        return "shell"
    if "read" in lowered or "view" in lowered or "cat" in lowered:
        return "read"
    if "edit" in lowered or "patch" in lowered or "replace" in lowered:
        return "edit"
    if "write" in lowered or "create_file" in lowered or "save" in lowered:
        return "write"
    if "search" in lowered or "grep" in lowered or "find" in lowered or "glob" in lowered:
        return "search"
    if "web" in lowered or "fetch" in lowered or "http" in lowered or "browse" in lowered:
        return "web"
    if lowered.startswith("mcp") or "mcp_" in lowered:
        return "mcp"
    return "other"


@dataclass
class ToolCallDetail:
    """Normalized tool-call view across providers."""

    tool_name: str
    kind: str = ""
    args_summary: str = ""
    call_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind or self.kind not in TOOL_KINDS:
            self.kind = normalize_tool_kind(self.tool_name or "")
            if self.kind not in TOOL_KINDS:
                raise AdapterEventError("tool kind %r not in TOOL_KINDS" % self.kind)


@dataclass
class AgentStreamEvent:
    """One normalized adapter-edge stream event (variant + payload)."""

    variant: str
    payload: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    turn_id: str = ""

    def __post_init__(self) -> None:
        if self.variant not in EVENT_VARIANTS:
            raise AdapterEventError(
                "unknown stream event variant: %r (allowed: %s)"
                % (self.variant, "|".join(EVENT_VARIANTS))
            )


@dataclass
class AgentCapabilityFlags:
    """Dual-channel capability gating: serialized into server_info
    features; clients read flags, the manager falls back on absence.
    `context_compaction` is harness-owned (Paseo has no equivalent)."""

    streaming: bool = False
    tool_use: bool = False
    session_resume: bool = False
    thinking: bool = False
    images: bool = False
    context_compaction: bool = False

    def to_features(self) -> Dict[str, bool]:
        return {
            "streaming": self.streaming,
            "tool_use": self.tool_use,
            "session_resume": self.session_resume,
            "thinking": self.thinking,
            "images": self.images,
            "context_compaction": self.context_compaction,
        }

    @classmethod
    def from_features(cls, features: Optional[Dict[str, Any]]) -> "AgentCapabilityFlags":
        """Dual-channel fallback: absent flag = False (manager decides the
        fallback behavior; no fallback paths inside features)."""
        features = features or {}
        return cls(
            streaming=bool(features.get("streaming", False)),
            tool_use=bool(features.get("tool_use", False)),
            session_resume=bool(features.get("session_resume", False)),
            thinking=bool(features.get("thinking", False)),
            images=bool(features.get("images", False)),
            context_compaction=bool(features.get("context_compaction", False)),
        )


@runtime_checkable
class AgentSession(Protocol):
    """One live conversation with one provider. Auth is the client's
    concern; the session only carries its opaque resume handle."""

    def session_handle(self) -> str: ...

    async def send_prompt(self, prompt: str) -> AsyncIterator[AgentStreamEvent]: ...

    async def interrupt(self) -> bool: ...


@runtime_checkable
class AgentClient(Protocol):
    """Provider boundary: manages its own auth, mints and resumes
    sessions. Never touches budget funnels / compaction."""

    def capabilities(self) -> AgentCapabilityFlags: ...

    async def new_session(self, session_id: str) -> AgentSession: ...

    async def resume_session(self, session_id: str, handle: str) -> AgentSession: ...
