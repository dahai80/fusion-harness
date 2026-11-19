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


class SandboxRunnerUnavailable(RuntimeError):
    """Reference runner could not set up isolation (e.g. sandbox-exec
    missing). Consumers must treat this as deny, never run raw."""


# Issue #90 / S1: minimal reference runner for macOS — the POSIX profile
# forbids file writes outside temp dirs and network. Best-effort: hosts
# with stricter appetites should ship their own runner and pass the same
# conformance suite. Consumers on non-mac platforms get
# SandboxRunnerUnavailable and must deny.
_SANDBOX_EXEC_PROFILE = (
    "(version 1)(deny network*)(deny file-write*)(allow file-read*)(allow process*)"
)


class SandboxExecRunner:
    """Reference SandboxRunner: macOS sandbox-exec with a deny-write/
    deny-network profile. Pure stdlib. run_sandboxed is async per the
    SandboxRunner protocol; sandbox-exec absence raises
    SandboxRunnerUnavailable (fail-closed at the caller)."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s

    async def run_sandboxed(self, tool_name: str, args: Dict[str, Any]) -> Any:
        command = str(args.get("command") or "")
        if not command:
            raise SandboxRunnerUnavailable("no command in args for %s" % tool_name)
        import asyncio
        import shutil

        binary = shutil.which("sandbox-exec")
        if binary is None:
            raise SandboxRunnerUnavailable("sandbox-exec not available on this host")
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-p",
            _SANDBOX_EXEC_PROFILE,
            "/bin/sh",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except TimeoutError as exc:
            proc.kill()
            raise SandboxRunnerUnavailable(
                "sandboxed call timed out after %.0fs" % self.timeout_s
            ) from exc
        if proc.returncode != 0:
            raise SandboxRunnerUnavailable(
                "sandboxed call failed rc=%s err=%s"
                % (proc.returncode, stderr.decode(errors="replace")[:200])
            )
        return stdout.decode(errors="replace")


def check_runner_conformance(runner: Any) -> list:
    """Structural conformance checks any SandboxRunner must pass (issue #90
    S1): async run_sandboxed with the protocol signature. Returns a list of
    violations — empty means conformant. Behavioral conformance (deny
    without runner, tier routing, failure-deny) lives in the shared test
    suite tests/test_sandbox_conformance.py."""
    violations = []
    if not callable(getattr(runner, "run_sandboxed", None)):
        violations.append("run_sandboxed missing or not callable")
        return violations
    import inspect

    if not inspect.iscoroutinefunction(runner.run_sandboxed):
        violations.append("run_sandboxed must be async (protocol returns Awaitable)")
    return violations


__all__ = [
    "ExecutionTier",
    "SandboxRunner",
    "SandboxPolicy",
    "SandboxDecision",
    "RISK_BAND_TIERS",
    "SandboxExecRunner",
    "SandboxRunnerUnavailable",
    "check_runner_conformance",
    "map_risk_to_tier",
]
