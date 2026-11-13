"""Security-intel client (issue #24, fusion-security P2.2 contract).

Harness stays the deny-first executor; fusion-security is the intel
provider. Opt-in: when no client is wired (or the endpoint is unreachable)
behavior is exactly today's — graceful no-op, harness keeps working
offline. Hints from the intel endpoint are ADVISORY by default; a consumer
may set enforce_hints to turn a "deny" hint into a funnel denial.

Wire contract: schemas/agent-risk-score.v1.json in fusion-security (PR #89)
— POST /api/v1/agent/risk-score, POST /api/v1/agent/decisions.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_HINTS = ("allow", "require_approval", "sandbox", "deny")


@dataclass
class RiskIntel:
    tool_name: str
    score: int
    risk_class: str
    policy_hints: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)


class RiskIntelClient:
    def __init__(
        self,
        base_url: str,
        request_timeout: float = 2.0,
        tenant_id: str = "default",
        caller: Optional[Dict[str, Any]] = None,
        token: str = "",
        enforce_hints: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.tenant_id = tenant_id
        self.caller = caller or {}
        self.token = token
        self.enforce_hints = enforce_hints

    async def fetch_risk(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[RiskIntel]:
        """Ask fusion-security for risk intel. Any failure returns None —
        intel is an upgrade, never a dependency (offline-first)."""
        payload = {
            "tool_name": tool_name,
            "arguments": arguments or {},
            "caller": self.caller,
            "tenant_id": self.tenant_id,
        }
        try:
            body = await asyncio.to_thread(self._post_blocking, "/api/v1/agent/risk-score", payload)
        except Exception as exc:
            logger.warning("risk intel unavailable tool=%s err=%s", tool_name, exc)
            return None
        hints = [h for h in body.get("policy_hints") or [] if h in VALID_HINTS]
        intel = RiskIntel(
            tool_name=str(body.get("tool_name") or tool_name),
            score=int(body.get("score") or 0),
            risk_class=str(body.get("risk_class") or "low"),
            policy_hints=hints,
            signals=[str(s) for s in body.get("signals") or []],
        )
        logger.info(
            "risk intel tool=%s score=%d class=%s hints=%s",
            intel.tool_name,
            intel.score,
            intel.risk_class,
            intel.policy_hints,
        )
        return intel

    async def push_decision(
        self, tool_name: str, decision: str, reason: str = "", trace_id: str = ""
    ) -> bool:
        """Push the harness's own allow/deny back into the security audit
        stream (closed feedback loop). Failure is a logged no-op."""
        if decision not in ("allow", "deny"):
            logger.error("invalid decision push tool=%s decision=%s", tool_name, decision)
            return False
        payload = {
            "tool_name": tool_name,
            "decision": decision,
            "caller": self.caller,
            "reason": reason,
            "tenant_id": self.tenant_id,
            "trace_id": trace_id,
        }
        try:
            await asyncio.to_thread(self._post_blocking, "/api/v1/agent/decisions", payload)
        except Exception as exc:
            logger.warning("decision push failed tool=%s err=%s", tool_name, exc)
            return False
        logger.info("decision pushed tool=%s decision=%s", tool_name, decision)
        return True

    def _post_blocking(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            resp = urllib.request.urlopen(req, timeout=self.request_timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError("http %s: %s" % (exc.code, detail)) from exc
        except OSError as exc:
            raise RuntimeError("unreachable: %s" % exc) from exc
        with resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))


__all__ = ["RiskIntel", "RiskIntelClient"]
