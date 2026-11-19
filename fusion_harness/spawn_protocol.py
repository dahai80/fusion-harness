"""Spawn brief contract + deterministic effort tiers (issue #26, P2).

Vague briefs are treated as bugs: every worker spawn carries a complete
contract (objective / output_format / tool_guidance / boundaries /
token_budget / concurrency / depth), validated by a pure function —
incomplete briefs raise a typed error, never a silent degrade. Effort
scaling is deterministic config, not model improvisation: tier rules live
in code (Anthropic hardcodes them in the lead prompt; MAST 2503.13657
measures +9.4% success from better role specification).
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SpawnBriefError(TypeError):
    """Raised when a spawn brief is incomplete or malformed (fail visibly)."""


REQUIRED_FIELDS = (
    "objective",
    "output_format",
    "tool_guidance",
    "boundaries",
    "token_budget",
    "concurrency",
    "depth",
)

# Issue #124: empirically derived fan-out ladder (Anthropic multi-agent
# research system): 3-5 subagents for simple objectives, 10-15 for complex,
# hard max 20; multi-agent runs cost ~15x single-chat tokens (research
# agents ~4x coding agents). Deterministic check, no model call.
FANOUT_SIMPLE_MAX = 5
FANOUT_COMPLEX_MAX = 15
FANOUT_HARD_MAX = 20
MULTI_AGENT_TOKEN_MULTIPLIER = 15
TOKEN_MULTIPLIER_WARNING = (
    "multi-agent fan-out costs ~%dx single-chat tokens (research agents ~4x "
    "coding agents) — budget accordingly" % MULTI_AGENT_TOKEN_MULTIPLIER
)


@dataclass
class SpawnBrief:
    objective: str
    output_format: str
    tool_guidance: str
    boundaries: str
    token_budget: int
    concurrency: int
    depth: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        errors = validate_brief(self)
        if errors:
            raise SpawnBriefError("incomplete spawn brief: " + "; ".join(errors))
        fanout_warnings(self)


def validate_brief(brief: Any) -> List[str]:
    """Pure validator: returns a list of contract violations (empty = valid).
    Usable as a lint hook on spawn sites and on lifecycle events."""
    errors = []
    for name in ("objective", "output_format", "tool_guidance", "boundaries"):
        value = getattr(brief, name, None)
        if not isinstance(value, str) or not value.strip():
            errors.append("%s must be a non-empty string" % name)
    for name in ("token_budget", "concurrency", "depth"):
        value = getattr(brief, name, None)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append("%s must be a positive integer" % name)
    if getattr(brief, "depth", 0) and getattr(brief, "depth", 0) > 3:
        errors.append("depth must be <= 3 (orchestration ladder guard)")
    concurrency = getattr(brief, "concurrency", 0)
    if (
        isinstance(concurrency, int)
        and not isinstance(concurrency, bool)
        and concurrency > FANOUT_HARD_MAX
    ):
        errors.append(
            "concurrency %d exceeds fan-out hard max %d (Anthropic multi-agent ladder)"
            % (concurrency, FANOUT_HARD_MAX)
        )
    return errors


def fanout_warnings(brief: Any) -> List[str]:
    """Issue #124: non-fatal fan-out guidance. Returns warnings (empty = in
    band) — validate_brief still hard-errors above FANOUT_HARD_MAX."""
    warnings = []
    concurrency = getattr(brief, "concurrency", 0)
    if isinstance(concurrency, int) and not isinstance(concurrency, bool):
        if concurrency > FANOUT_COMPLEX_MAX:
            warnings.append(
                "concurrency %d above the complex-objective band (10-%d); %s"
                % (concurrency, FANOUT_COMPLEX_MAX, TOKEN_MULTIPLIER_WARNING)
            )
        elif FANOUT_SIMPLE_MAX < concurrency < 10:
            warnings.append(
                "concurrency %d between the simple (%d) and complex (10-%d) bands; %s"
                % (concurrency, FANOUT_SIMPLE_MAX, FANOUT_COMPLEX_MAX, TOKEN_MULTIPLIER_WARNING)
            )
    for warning in warnings:
        logger.warning("fanout guidance: %s", warning)
    return warnings


def brief_from_event(payload: Dict[str, Any]) -> SpawnBrief:
    """Build a SpawnBrief from a lifecycle event payload; raises
    SpawnBriefError on missing/malformed fields. Register on PRE_SPAWN-style
    hooks so a bad brief never reaches SubagentRunner.run()."""
    missing = [name for name in REQUIRED_FIELDS if name not in payload]
    if missing:
        raise SpawnBriefError("brief payload missing fields: " + ", ".join(missing))
    return SpawnBrief(
        objective=payload["objective"],
        output_format=payload["output_format"],
        tool_guidance=payload["tool_guidance"],
        boundaries=payload["boundaries"],
        token_budget=payload["token_budget"],
        concurrency=payload["concurrency"],
        depth=payload["depth"],
        metadata=payload.get("metadata") or {},
    )


@dataclass
class EffortTier:
    """Deterministic effort scaling decision (no model call involved)."""

    name: str
    workers: int
    split_responsibilities: bool
    reason: str


@dataclass
class EffortTierConfig:
    single_agent_max_calls: int = 3
    small_team_max_calls: int = 10
    small_team_min: int = 2
    small_team_max: int = 4
    large_team_min: int = 10
    max_calls_per_worker: int = 15

    def resolve(self, estimated_tool_calls: int) -> EffortTier:
        if estimated_tool_calls <= self.single_agent_max_calls:
            tier = EffortTier(
                name="single",
                workers=1,
                split_responsibilities=False,
                reason="<=%d calls: one agent, no coordination overhead"
                % self.single_agent_max_calls,
            )
        elif estimated_tool_calls <= self.small_team_max_calls:
            workers = max(
                self.small_team_min,
                min(self.small_team_max, estimated_tool_calls // self.max_calls_per_worker + 2),
            )
            tier = EffortTier(
                name="small_team",
                workers=workers,
                split_responsibilities=True,
                reason="3-10 calls: %d-%d subagents with explicit responsibilities"
                % (self.small_team_min, self.small_team_max),
            )
        else:
            workers = max(
                self.large_team_min,
                -(-estimated_tool_calls // self.max_calls_per_worker),
            )
            tier = EffortTier(
                name="large_split",
                workers=workers,
                split_responsibilities=True,
                reason=">10 calls: 10+ split with explicit responsibilities",
            )
        logger.info(
            "effort tier resolved calls=%d tier=%s workers=%d",
            estimated_tool_calls,
            tier.name,
            tier.workers,
        )
        return tier


__all__ = [
    "FANOUT_COMPLEX_MAX",
    "FANOUT_HARD_MAX",
    "FANOUT_SIMPLE_MAX",
    "MULTI_AGENT_TOKEN_MULTIPLIER",
    "TOKEN_MULTIPLIER_WARNING",
    "SpawnBrief",
    "SpawnBriefError",
    "validate_brief",
    "brief_from_event",
    "fanout_warnings",
    "EffortTier",
    "EffortTierConfig",
]
