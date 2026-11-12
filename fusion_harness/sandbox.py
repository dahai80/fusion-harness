"""Sandbox execution tiers (plan P3-3, gap G8).

Permission output extended with an enforcement tier: `process` (default),
`sandbox` (consumer-provided runner — macOS sandbox-exec/VM profile), or
`blocked`. Fail-closed rules:
- a tool mapped to SANDBOX with no runner wired is DENIED, never run raw
- `bypassPermissions` requires a sandbox runner (aligning Claude Code
  semantics: bypass only inside isolation)
Sandbox is an enforcement mechanism, not a second policy engine — the
permission engine's decisions are unchanged; only routing adds a tier.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class ExecutionTier(str, Enum):
    PROCESS = "process"
    SANDBOX = "sandbox"
    BLOCKED = "blocked"


# Risk band (from permissions.score_tool_risk) -> default execution tier.
RISK_BAND_TIERS: Dict[str, ExecutionTier] = {
    "destructive": ExecutionTier.SANDBOX,
    "network": ExecutionTier.PROCESS,
    "write": ExecutionTier.PROCESS,
    "read": ExecutionTier.PROCESS,
}

DEFAULT_TIER = ExecutionTier.PROCESS


@runtime_checkable
class SandboxRunner(Protocol):
    def run_sandboxed(self, tool_name: str, args: Dict[str, Any]) -> Awaitable[Any]: ...


def map_risk_to_tier(risk_score: float, band: str = "") -> ExecutionTier:
    """Deterministic risk->tier mapping. Destructive band routes to sandbox;
    consumers may override via policy but never below their risk appetite."""
    if band and band in RISK_BAND_TIERS:
        return RISK_BAND_TIERS[band]
    if risk_score >= 0.9:
        return ExecutionTier.SANDBOX
    return DEFAULT_TIER


@dataclass
class SandboxDecision:
    tier: ExecutionTier
    reason: str = ""


class SandboxPolicy:
    """Maps (tool, args, risk) -> tier; wired into ToolFunnel as the routing
    step between admission and execution."""

    def __init__(
        self,
        runner: Optional[SandboxRunner] = None,
        tool_tiers: Optional[Dict[str, ExecutionTier]] = None,
        risk_threshold: float = 0.9,
    ) -> None:
        self.runner = runner
        self.tool_tiers = tool_tiers or {}
        self.risk_threshold = risk_threshold

    def decide(
        self, tool_name: str, args: Dict[str, Any], risk: tuple[float, str]
    ) -> SandboxDecision:
        score, band = risk
        tier = self.tool_tiers.get(tool_name)
        if tier is None:
            if score >= self.risk_threshold:
                tier = ExecutionTier.SANDBOX
            elif band in RISK_BAND_TIERS:
                tier = RISK_BAND_TIERS[band]
            else:
                tier = DEFAULT_TIER
        if tier is ExecutionTier.BLOCKED:
            return SandboxDecision(tier=tier, reason="tool mapped to blocked tier")
        if tier is ExecutionTier.SANDBOX and self.runner is None:
            logger.error("sandbox required but no runner wired tool=%s — deny", tool_name)
            return SandboxDecision(
                tier=ExecutionTier.BLOCKED, reason="sandbox tier required but no runner wired"
            )
        return SandboxDecision(tier=tier)


__all__ = [
    "ExecutionTier",
    "SandboxRunner",
    "SandboxPolicy",
    "SandboxDecision",
    "RISK_BAND_TIERS",
    "map_risk_to_tier",
]
