"""Team entity definition (issue #31, T1).

Kernel owns DETERMINISTIC mechanisms only: definition validation, task-board
state machine, message routing, state persistence. Zero model calls in any
TeamRuntime API (CI-greppable). Judgment (task assignment, lead decisions,
stall detection, re-planning) stays consumer-side.

Design doc: fusion-insight/fusion-team-architecture-2026-11-14.md.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VALID_MODES = ("bypass", "approved", "default", "plan")

# Issue #141: per-member model-tier vocabulary. The kernel never resolves
# the hint to an actual model — consumers own the mapping (kernel stays
# model-agnostic, zero model calls, deterministic). "" = unspecified.
VALID_MODEL_TIERS = ("", "cheap", "strong")

# Per-member privacy routing constraint. "local-only" forbids dispatching
# the member's model traffic to anything but the local endpoint; the kernel
# only carries the constraint (spec.metadata["privacy"]) — consumers own
# enforcement (kernel stays model-agnostic). "" = unspecified.
VALID_PRIVACY_MODES = ("", "local-only")


class TeamDefError(TypeError):
    """Raised on illegal team definitions (fail visibly)."""


@dataclass
class TeamMember:
    name: str
    role: str
    skills: List[str] = field(default_factory=list)
    tool_allowlist: List[str] = field(default_factory=list)
    permission_mode: str = "default"
    budget_tokens: int = 50_000
    max_concurrency: int = 1
    context: str = ""
    model_tier: str = ""
    privacy: str = ""

    def __post_init__(self) -> None:
        if not _MEMBER_NAME_RE.match(self.name or ""):
            raise TeamDefError("member name must match [A-Za-z0-9_-]+: %r" % self.name)
        if not self.role or not isinstance(self.role, str):
            raise TeamDefError("member role required: %s" % self.name)
        if self.permission_mode not in _VALID_MODES:
            raise TeamDefError("unknown permission mode: %s" % self.permission_mode)
        if self.budget_tokens <= 0 or self.max_concurrency <= 0:
            raise TeamDefError("budget_tokens and max_concurrency must be positive: %s" % self.name)
        if self.model_tier not in VALID_MODEL_TIERS:
            raise TeamDefError(
                "unknown model tier %r (valid: cheap, strong, or empty)" % self.model_tier
            )
        if self.privacy not in VALID_PRIVACY_MODES:
            raise TeamDefError(
                "unknown privacy mode %r (valid: local-only, or empty)" % self.privacy
            )


@dataclass
class TeamDefinition:
    name: str
    namespace_key: str
    members: List[TeamMember]
    lead_budget_tokens: int = 200_000
    playbook: bool = False
    max_depth: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise TeamDefError("team name required")
        if not self.namespace_key or not isinstance(self.namespace_key, str):
            raise TeamDefError("namespace_key required (memory anchor)")
        if not self.members:
            raise TeamDefError("team needs at least one member")
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise TeamDefError("duplicate member names: %s" % names)
        if self.max_depth < 1 or self.max_depth > 3:
            raise TeamDefError("max_depth must be in 1..3 (global ladder cap)")
        for member in self.members:
            if member.budget_tokens > self.lead_budget_tokens:
                raise TeamDefError(
                    "member %s budget %d exceeds lead budget %d (Deadline.child semantics)"
                    % (member.name, member.budget_tokens, self.lead_budget_tokens)
                )
            if member.model_tier not in VALID_MODEL_TIERS:
                raise TeamDefError(
                    "member %s has unknown model tier %r (valid: cheap, strong, or empty)"
                    % (member.name, member.model_tier)
                )

    @property
    def memory_anchor(self) -> Dict[str, str]:
        return {"tier": "team", "key": self.namespace_key}

    def member(self, name: str) -> TeamMember:
        for m in self.members:
            if m.name == name:
                return m
        raise TeamDefError("unknown member: %s" % name)


__all__ = ["TeamMember", "TeamDefinition", "TeamDefError", "VALID_MODEL_TIERS"]
