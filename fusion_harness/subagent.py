"""Subagent runtime primitive (plan P2-1, gap G3).

One kernel primitive — not a fleet framework. A subagent is an isolated child
execution with its own context, scoped tool surface, tightened permission
mode, capped budget, and summary-only return. Orchestration ladders
(background, teams, workflows) stay consumer-side on top of this.

Child narrowing rules (mirror Deadline.child() semantics):
- permission mode can only get stricter (bypass > approved > default > plan)
- tool surface is the parent's intersected with the spec's allowlist
- budget caps are additional, never lifted
Only the summary event crosses back to the parent bus.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fusion_harness.funnels import LifecycleBus

logger = logging.getLogger(__name__)

_MODE_STRICTNESS = {"bypass": 0, "approved": 1, "default": 2, "plan": 3}


@dataclass
class SubagentSpec:
    name: str
    prompt: str
    tool_allowlist: List[str] = field(default_factory=list)
    permission_mode: str = "default"
    budget_tokens: int = 50_000
    max_turns: int = 20
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("subagent name is required")
        if not self.prompt:
            raise ValueError("subagent prompt is required")
        if self.permission_mode not in _MODE_STRICTNESS:
            raise ValueError("unknown permission mode: %s" % self.permission_mode)
        if self.budget_tokens <= 0 or self.max_turns <= 0:
            raise ValueError("budget_tokens and max_turns must be positive")


@dataclass
class SubagentResult:
    subagent_id: str
    name: str
    status: str
    summary: str = ""
    turns: int = 0
    tokens_spent: int = 0
    error: str = ""


AgentFn = Callable[[SubagentSpec, "SubagentSession"], Awaitable[str]]


@dataclass
class SubagentSession:
    """Handle handed to the agent function: the isolated child surface."""

    subagent_id: str
    spec: SubagentSpec
    allowed_tools: frozenset
    permission_mode: str
    budget_tokens: int
    ledger: Any = None
    max_turns: int = 20
    turns: int = 0
    tokens_spent: int = 0
    _violations: List[str] = field(default_factory=list)

    def record_turn(self, tokens: int = 0) -> str:
        """Advance one turn; returns 'ok', 'budget_exceeded', or 'max_turns'.
        Token accounting goes through the TokenLedger (single ownership,
        spec §3.4) when one is wired; the cap is enforced on the ledger
        balance, never on a mutable drift-prone field."""
        self.turns += 1
        tokens = max(0, tokens)
        if self.ledger is not None and tokens:
            self.ledger.append(scope=self.subagent_id, completion_tokens=tokens)
            bal = self.ledger.balance(scope=self.subagent_id)
            self.tokens_spent = int(bal.get("completion_tokens", 0)) + int(
                bal.get("prompt_tokens", 0)
            )
        else:
            self.tokens_spent += tokens
        if self.tokens_spent > self.budget_tokens:
            self._violations.append("budget_exceeded")
            return "budget_exceeded"
        if self.turns >= self.max_turns:
            self._violations.append("max_turns")
            return "max_turns"
        return "ok"

    def check_tool(self, tool_name: str) -> bool:
        """Tool-surface check: the child may only use its allowlist."""
        return tool_name in self.allowed_tools

    @property
    def violations(self) -> List[str]:
        return list(self._violations)


class SubagentRunner:
    def __init__(
        self,
        bus: Optional[LifecycleBus] = None,
        clock: Any = time.time,
        ledger_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.bus = bus
        self._clock = clock
        self.ledger_factory = ledger_factory
        self._counter = 0

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                "subagent", event, payload, session_id=str(payload.get("subagent_id") or "")
            )
        except Exception as exc:
            logger.warning("subagent lifecycle emit failed: %s", exc)

    @staticmethod
    def narrow_tools(parent_tools: List[str], spec: SubagentSpec) -> frozenset:
        """Child surface = parent surface ∩ spec allowlist (empty allowlist = parent surface)."""
        if spec.tool_allowlist:
            return frozenset(set(parent_tools) & set(spec.tool_allowlist))
        return frozenset(parent_tools)

    @staticmethod
    def narrow_mode(parent_mode: str, spec: SubagentSpec) -> str:
        """Child mode = stricter of parent and spec (never widens)."""
        parent = parent_mode if parent_mode in _MODE_STRICTNESS else "default"
        if _MODE_STRICTNESS[spec.permission_mode] >= _MODE_STRICTNESS[parent]:
            return spec.permission_mode
        logger.warning(
            "subagent mode narrowed: spec=%s parent=%s -> %s",
            spec.permission_mode,
            parent,
            parent,
        )
        return parent

    @staticmethod
    def child_engine(parent_engine: Any, spec: SubagentSpec) -> Any:
        """Child PermissionEngine: inherits every parent rule, tightens mode,
        adds node-layer allow rules from the spec's allowlist. A child can
        narrow, never widen — inherits parent snapshot verbatim."""
        from fusion_harness.permissions import PermissionEngine, PermissionRule

        rules = list(parent_engine.rules_snapshot()) if parent_engine is not None else []
        for tool in spec.tool_allowlist:
            pattern = tool if tool.endswith("*") else "^" + re.escape(tool) + "$"
            rules.append(
                PermissionRule(
                    action="allow",
                    tool=pattern,
                    layer="node",
                    description="derived from subagent spec %s" % spec.name,
                )
            )
        raw_mode = getattr(parent_engine, "mode", "default")
        parent_mode = getattr(raw_mode, "value", raw_mode)
        return PermissionEngine(
            rules=rules, mode=SubagentRunner.narrow_mode(str(parent_mode), spec)
        )

    async def run(
        self,
        spec: SubagentSpec,
        agent_fn: AgentFn,
        parent_tools: Optional[List[str]] = None,
        parent_mode: str = "default",
    ) -> SubagentResult:
        self._counter += 1
        subagent_id = "subagent-%d" % self._counter
        allowed = self.narrow_tools(parent_tools or [], spec)
        mode = self.narrow_mode(parent_mode, spec)
        session = SubagentSession(
            subagent_id=subagent_id,
            spec=spec,
            allowed_tools=allowed,
            permission_mode=mode,
            budget_tokens=spec.budget_tokens,
            ledger=self.ledger_factory() if self.ledger_factory else None,
            max_turns=spec.max_turns,
        )
        started = self._clock()
        self._emit("subagent_start", {"subagent_id": subagent_id, "name": spec.name, "mode": mode})
        logger.info(
            "subagent start id=%s name=%s tools=%d mode=%s budget=%d",
            subagent_id,
            spec.name,
            len(allowed),
            mode,
            spec.budget_tokens,
        )
        try:
            summary = str(await agent_fn(spec, session) or "")
        except Exception as exc:
            logger.exception("subagent failed id=%s name=%s", subagent_id, spec.name)
            result = SubagentResult(
                subagent_id=subagent_id,
                name=spec.name,
                status="failed",
                turns=session.turns,
                tokens_spent=session.tokens_spent,
                error=str(exc),
            )
            self._emit(
                "subagent_end", {"subagent_id": subagent_id, "status": "failed", "error": str(exc)}
            )
            return result
        status = "capped" if session.violations else "completed"
        result = SubagentResult(
            subagent_id=subagent_id,
            name=spec.name,
            status=status,
            summary=summary,
            turns=session.turns,
            tokens_spent=session.tokens_spent,
        )
        self._emit(
            "subagent_end",
            {
                "subagent_id": subagent_id,
                "status": status,
                "turns": session.turns,
                "tokens": session.tokens_spent,
                "elapsed": round(self._clock() - started, 3),
            },
        )
        logger.info(
            "subagent end id=%s status=%s turns=%d tokens=%d",
            subagent_id,
            status,
            session.turns,
            session.tokens_spent,
        )
        return result


__all__ = ["SubagentRunner", "SubagentSpec", "SubagentResult", "SubagentSession", "AgentFn"]
