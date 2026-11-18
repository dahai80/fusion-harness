"""Host-owned approval gate for destructive tool calls (issue #73).

Split-approval designs let destructive calls slip through the seam: a
tool-server-side confirm is treated as authorization (arXiv 2611.02527).
This gate pins ownership of destructive-call approval to the host kernel:

- classification is table-driven + deterministic name-token heuristics;
  destructive = mutates state outside the run workspace or irreversible
  (delete / force-push / payment-class tools).
- tool-server confirmation fields in args NEVER count as approval — a
  destructive call always needs a host approval token.
- the token is issued from the #66 pending-question queue, idempotent per
  call id, single-redemption; replay across call ids is rejected.
- every decision (granted / denied / timeout / replay) emits an English
  lifecycle telemetry event. Zero model calls, pure stdlib.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-tool policy table: exact tool name -> classification reason. Tools
# listed here are destructive regardless of arguments.
TOOL_DESTRUCTIVE_TABLE = {
    "shell.rm": "irreversible filesystem delete",
    "shell.rmdir": "irreversible filesystem delete",
    "fs.delete": "irreversible filesystem delete",
    "fs.truncate": "irreversible content destroy",
    "db.drop_table": "irreversible schema destroy",
    "db.drop_database": "irreversible data destroy",
    "git.force_push": "irreversible history rewrite",
    "git.push --force": "irreversible history rewrite",
    "git.reset --hard": "irreversible worktree discard",
    "payment.charge": "money-moving irreversible charge",
    "payment.refund": "money-moving irreversible refund",
    "dns.delete_record": "irreversible infra mutation",
    "deploy.destroy": "irreversible infra destroy",
    "storage.purge_bucket": "irreversible bulk delete",
}

# Deterministic name-token heuristics: a tool whose name contains one of
# these tokens is classified destructive even when not in the table.
DESTRUCTIVE_NAME_TOKENS = (
    "delete",
    "remove",
    "drop",
    "purge",
    "destroy",
    "truncate",
    "force_push",
    "force-push",
    "force_delete",
    "payment",
    "charge",
    "refund",
    "revoke",
)

# Tool-server-side confirmation fields — they can NEVER substitute for
# host approval (split-approval seam, arXiv 2611.02527).
SERVER_CONFIRM_KEYS = (
    "server_confirmed",
    "tool_server_confirmed",
    "server_confirmation",
    "confirmed",
    "confirm",
    "confirmed_by_server",
)

OUTCOME_GRANTED = "granted"
OUTCOME_DENIED = "denied"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_REPLAY = "replay_rejected"
OUTCOME_NOT_DESTRUCTIVE = "not_destructive"


@dataclass
class ApprovalDecision:
    call_id: str
    tool_name: str
    destructive: bool
    outcome: str
    reason: str = ""
    token: str = ""
    question_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "destructive": self.destructive,
            "outcome": self.outcome,
            "reason": self.reason,
            "question_id": self.question_id,
        }


class ApprovalGate:
    """Host-owned destructive-call approval path. Questions live in the
    #66 HumanGate queue (host is the requester); approval mints an
    idempotent, single-redemption token bound to one call id."""

    def __init__(
        self,
        human_gate,
        bus=None,
        clock: Any = time.time,
        ttl_s: float = 600.0,
        permission_engine=None,
    ) -> None:
        self.human_gate = human_gate
        self.bus = bus
        self._clock = clock or time.time
        self.ttl_s = ttl_s
        self.permission_engine = permission_engine
        self._questions: Dict[str, str] = {}
        self._tokens: Dict[str, str] = {}
        self._token_owners: Dict[str, str] = {}
        self._used: Dict[str, str] = {}
        self._final: Dict[str, ApprovalDecision] = {}
        self._locks: Dict[str, asyncio.Event] = {}

    # ---------- classification ----------

    @staticmethod
    def classify(tool_name: str, args: Any = None) -> Tuple[bool, str]:
        name = str(tool_name or "").lower()
        if str(tool_name or "") in TOOL_DESTRUCTIVE_TABLE:
            return True, TOOL_DESTRUCTIVE_TABLE[str(tool_name)]
        for token in DESTRUCTIVE_NAME_TOKENS:
            if token in name:
                return True, "destructive name token '%s'" % token
        return False, ""

    # ---------- host approval flow ----------

    def request(
        self,
        call_id: str,
        tool_name: str,
        session_id: str,
        args: Any = None,
    ) -> ApprovalDecision:
        """Open the host approval flow for a call. Non-destructive calls
        pass through. Destructive calls are ALWAYS evaluated by the kernel
        permission engine first; a server-side confirmation field in args
        is ignored by construction — it can never grant."""
        destructive, reason = self.classify(tool_name, args)
        if not destructive:
            decision = ApprovalDecision(
                call_id=call_id,
                tool_name=tool_name,
                destructive=False,
                outcome=OUTCOME_NOT_DESTRUCTIVE,
            )
            self._final[call_id] = decision
            return decision
        engine = self.permission_engine
        if engine is not None:
            try:
                verdict = engine.evaluate(tool_name, args or {})
                deny = verdict is not None and getattr(verdict, "decision", verdict) == "deny"
            except Exception as exc:
                logger.warning("approval gate permission engine error: %s", exc)
                deny = True
            if deny:
                decision = ApprovalDecision(
                    call_id=call_id,
                    tool_name=tool_name,
                    destructive=True,
                    outcome=OUTCOME_DENIED,
                    reason="denied by kernel permission engine",
                )
                self._final[call_id] = decision
                self._emit("destructive_denied", decision, session_id)
                logger.info("destructive call denied by engine call=%s tool=%s", call_id, tool_name)
                return decision
        question = self.human_gate.ask(
            session_id,
            "approve destructive call %s (%s)?" % (call_id, tool_name),
            payload={"call_id": call_id, "tool_name": tool_name},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            policy="block",
            requester="host",
            ttl_s=self.ttl_s,
        )
        self._questions[call_id] = question.id
        self._locks.setdefault(call_id, asyncio.Event())
        logger.info(
            "destructive call pending host approval call=%s tool=%s question=%s",
            call_id,
            tool_name,
            question.id,
        )
        return ApprovalDecision(
            call_id=call_id,
            tool_name=tool_name,
            destructive=True,
            outcome="pending",
            reason=reason,
            question_id=question.id,
        )

    async def decide(self, call_id: str, timeout: Optional[float] = None) -> ApprovalDecision:
        """Park on the #66 queue for the host's answer. Exactly one of
        granted / denied / timeout comes back; a finalized call returns its
        cached decision without re-emitting telemetry."""
        if call_id in self._final:
            return self._final[call_id]
        question_id = self._questions.get(call_id)
        if question_id is None:
            raise ValueError("no pending approval for call %s" % call_id)
        reply = await self.human_gate.wait(question_id, timeout)
        question = self.human_gate.get_question(question_id)
        tool_name = (question.payload.get("tool_name", "") if question else "") or ""
        session_id = question.session_id if question else ""
        if reply is None:
            self.human_gate.cancel(question_id)
            decision = ApprovalDecision(
                call_id=call_id,
                tool_name=tool_name,
                destructive=True,
                outcome=OUTCOME_TIMEOUT,
                reason="host approval not given before deadline",
                question_id=question_id,
            )
        elif reply.get("approved") is True:
            token = self.issue_token(call_id)
            decision = ApprovalDecision(
                call_id=call_id,
                tool_name=tool_name,
                destructive=True,
                outcome=OUTCOME_GRANTED,
                reason="host approved via question queue",
                token=token,
                question_id=question_id,
            )
        else:
            decision = ApprovalDecision(
                call_id=call_id,
                tool_name=tool_name,
                destructive=True,
                outcome=OUTCOME_DENIED,
                reason="host denied via question queue",
                question_id=question_id,
            )
        self._final[call_id] = decision
        event = {
            OUTCOME_GRANTED: "destructive_granted",
            OUTCOME_DENIED: "destructive_denied",
            OUTCOME_TIMEOUT: "destructive_timeout",
        }[decision.outcome]
        self._emit(event, decision, session_id)
        logger.info("destructive approval %s call=%s tool=%s", decision.outcome, call_id, tool_name)
        return decision

    # ---------- tokens ----------

    def issue_token(self, call_id: str) -> str:
        """Idempotent: the same call id always gets the same token."""
        token = self._tokens.get(call_id)
        if token is None:
            token = "apt_%s" % uuid.uuid4().hex[:16]
            self._tokens[call_id] = token
            self._token_owners[token] = call_id
            logger.info("approval token issued call=%s", call_id)
        return token

    def redeem(self, token: str, call_id: str) -> Dict[str, Any]:
        """Single redemption, bound to the issuing call id. A replay for a
        different call id (or a second use) is rejected loudly."""
        owner = self._token_owners.get(token)
        if owner is None:
            return {"redeemed": False, "reason": "unknown token"}
        if owner != call_id:
            logger.warning(
                "approval token replay rejected token=%s owner=%s attempted=%s",
                token,
                owner,
                call_id,
            )
            self._emit_replay(token, owner, call_id)
            return {"redeemed": False, "reason": OUTCOME_REPLAY}
        if token in self._used:
            logger.warning("approval token reuse rejected token=%s call=%s", token, call_id)
            return {"redeemed": False, "reason": OUTCOME_REPLAY}
        self._used[token] = call_id
        return {"redeemed": True, "reason": ""}

    def has_grant(self, call_id: str) -> bool:
        decision = self._final.get(call_id)
        return bool(decision and decision.outcome == OUTCOME_GRANTED and decision.token)

    def get_decision(self, call_id: str) -> Optional[ApprovalDecision]:
        return self._final.get(call_id)

    # ---------- telemetry ----------

    def _emit(self, event: str, decision: ApprovalDecision, session_id: str = "") -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                "approval",
                event,
                {
                    "call_id": decision.call_id,
                    "tool_name": decision.tool_name,
                    "outcome": decision.outcome,
                    "reason": decision.reason,
                },
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("approval telemetry emit failed: %s", exc)

    def _emit_replay(self, token: str, owner: str, attempted: str) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                "approval",
                "destructive_token_replay",
                {"token_owner_call": owner, "attempted_call": attempted},
                session_id="",
            )
        except Exception as exc:
            logger.warning("approval replay telemetry emit failed: %s", exc)


__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "DESTRUCTIVE_NAME_TOKENS",
    "OUTCOME_DENIED",
    "OUTCOME_GRANTED",
    "OUTCOME_NOT_DESTRUCTIVE",
    "OUTCOME_REPLAY",
    "OUTCOME_TIMEOUT",
    "SERVER_CONFIRM_KEYS",
    "TOOL_DESTRUCTIVE_TABLE",
]
