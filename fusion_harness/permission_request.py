"""Structured permission requests (issue #159, Paseo parity H8).

Paseo (v0.17.1, agent-sdk-types.ts) models a permission request as a
structured object: `kind` (tool | plan | question | mode | other) plus
`actions[]` each carrying an `intent` (implement | implement_resume |
dismiss), and the response may return a `followUpPrompt` that starts a
replacement turn (plan-approval-to-implementation flow).

This module adds that request/response model to the harness kernel as a
standalone, deterministic layer composable with `PermissionEngine` and
`HumanGate`. Backward compatible: a request with the single action
`behavior="allow"` (or `"deny"`) resolved by the matching response is
exactly the old allow/deny degenerate case.

Pure stdlib, deterministic, zero model calls.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

REQUEST_KINDS = ("tool", "plan", "question", "mode", "other")
ACTION_INTENTS = ("implement", "implement_resume", "dismiss")
DEGENERATE_BEHAVIORS = ("allow", "deny")

__all__ = [
    "ACTION_INTENTS",
    "DEGENERATE_BEHAVIORS",
    "PermissionRequest",
    "PermissionRequestError",
    "PermissionResponse",
    "REQUEST_KINDS",
    "RequestAction",
    "ResolvedRequest",
    "resolve_request",
]


class PermissionRequestError(ValueError):
    """Invalid structured permission request/response (fail visibly)."""


@dataclass
class RequestAction:
    """One selectable behavior with the turn-intent it carries."""

    behavior: str
    intent: str = "dismiss"

    def __post_init__(self) -> None:
        if not self.behavior:
            raise PermissionRequestError("action behavior is required")
        if self.intent not in ACTION_INTENTS:
            raise PermissionRequestError(
                "unknown action intent: %r (allowed: %s)" % (self.intent, "|".join(ACTION_INTENTS))
            )


@dataclass
class PermissionRequest:
    """Structured permission request (Paseo AgentPermissionRequest shape)."""

    session_id: str
    kind: str
    actions: List[RequestAction]
    prompt: str = ""
    suggestions: List[str] = field(default_factory=list)
    id: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "preq_%s" % uuid.uuid4().hex[:8]
        if not self.session_id:
            raise PermissionRequestError("session_id is required")
        if self.kind not in REQUEST_KINDS:
            raise PermissionRequestError(
                "unknown request kind: %r (allowed: %s)" % (self.kind, "|".join(REQUEST_KINDS))
            )
        if not self.actions:
            raise PermissionRequestError("at least one action is required")

    def find_action(self, behavior: str) -> RequestAction:
        for action in self.actions:
            if action.behavior == behavior:
                return action
        raise PermissionRequestError(
            "behavior %r not offered by request %s (offered: %s)"
            % (behavior, self.id, "|".join(a.behavior for a in self.actions))
        )


@dataclass
class PermissionResponse:
    """Human/host response: chosen behavior + optional replacement-turn
    prompt (plan-approval-to-implementation)."""

    request_id: str
    behavior: str
    follow_up_prompt: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedRequest:
    """Deterministic resolution outcome."""

    request_id: str
    behavior: str
    intent: str
    follow_up_prompt: str = ""
    # True when the resolution is exactly the legacy allow/deny case.
    degenerate: bool = False


def resolve_request(request: PermissionRequest, response: PermissionResponse) -> ResolvedRequest:
    """Resolve a structured response against its request. The chosen
    behavior must be one the request offered. A followUpPrompt is only
    honored for implement/intent actions (implement / implement_resume);
    dismiss drops it."""
    if response.request_id != request.id:
        raise PermissionRequestError(
            "response request_id %r does not match request %r" % (response.request_id, request.id)
        )
    action = request.find_action(response.behavior)
    follow_up = ""
    if response.follow_up_prompt:
        if action.intent in ("implement", "implement_resume"):
            follow_up = response.follow_up_prompt
        else:
            logger.warning(
                "followUpPrompt dropped: intent=%s is not an implement intent request=%s",
                action.intent,
                request.id,
            )
    degenerate = (
        len(request.actions) == 1
        and request.actions[0].behavior in DEGENERATE_BEHAVIORS
        and action.behavior == request.actions[0].behavior
    )
    resolved = ResolvedRequest(
        request_id=request.id,
        behavior=action.behavior,
        intent=action.intent,
        follow_up_prompt=follow_up,
        degenerate=degenerate,
    )
    logger.info(
        "permission request resolved id=%s behavior=%s intent=%s follow_up=%s",
        request.id,
        action.behavior,
        action.intent,
        bool(follow_up),
    )
    return resolved
