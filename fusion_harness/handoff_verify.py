"""Handoff verification: replay the sender's permission ledger (issue #53).

arXiv 2611.06545: when one agent hands off to another, the receiver
replays the sender's permission ledger against the claims in the
handoff payload — guards against handoff hallucination (sender claiming
actions it never performed or was denied). A mismatch surfaces as a
verification failure, never a silent accept. Ledger segments ride the
existing handoff transport (SpawnBrief.metadata / message payloads);
no new transport. The engine records every evaluate() outcome, so an
honest handoff costs only the replay pass.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class HandoffVerifyError(ValueError):
    """Malformed ledger or claims."""


@dataclass
class HandoffVerification:
    ok: bool
    failures: list = field(default_factory=list)
    checked: int = 0

    def report(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "failures": self.failures}


def verify_handoff(claims: list, ledger: list) -> HandoffVerification:
    """Replay claims against the sender's ledger segment.

    claims: [{"tool": "..."}] — actions the sender says it performed.
    ledger: [{"tool": "...", "decision": "allow"|"deny"|"ask", ...}].
    A claim passes only if the ledger records an ALLOW for that tool.
    Absent = fabricated; deny/ask = unauthorized. Both fail loudly.
    """
    if not isinstance(claims, list) or not isinstance(ledger, list):
        raise HandoffVerifyError("claims and ledger must be lists")
    allowed: dict = {}
    for entry in ledger:
        if not isinstance(entry, dict) or not entry.get("tool"):
            raise HandoffVerifyError("ledger entry missing 'tool'")
        if entry.get("decision") == "allow":
            allowed[entry["tool"]] = True
    out = HandoffVerification(ok=True)
    for claim in claims:
        if not isinstance(claim, dict) or not claim.get("tool"):
            raise HandoffVerifyError("claim missing 'tool'")
        tool = claim["tool"]
        out.checked += 1
        if tool not in allowed:
            recorded = any(e.get("tool") == tool for e in ledger)
            reason = "denied" if recorded else "not recorded"
            out.ok = False
            out.failures.append({"tool": tool, "reason": reason})
            logger.warning("handoff claim rejected tool=%s reason=%s", tool, reason)
    if out.ok:
        logger.info("handoff verified claims=%d", out.checked)
    return out


def ledger_for_transport(ledger: list) -> dict:
    """Wrap a ledger segment for the handoff payload (metadata field)."""
    return {"permission_ledger": list(ledger or [])}


def claims_for_transport(claims: list) -> dict:
    return {"handoff_claims": list(claims or [])}


def verify_spawn_brief(brief) -> HandoffVerification:
    """Receiver side for SpawnBrief-style metadata: both the ledger and
    the claims ride in brief.metadata; missing either is a failure, not
    a silent accept."""
    metadata = getattr(brief, "metadata", None) or {}
    ledger = metadata.get("permission_ledger")
    claims = metadata.get("handoff_claims")
    if ledger is None or claims is None:
        logger.warning("handoff payload missing ledger or claims — verification fails")
        return HandoffVerification(ok=False, failures=[{"reason": "missing ledger or claims"}])
    return verify_handoff(claims, ledger)


__all__ = [
    "HandoffVerifyError",
    "HandoffVerification",
    "claims_for_transport",
    "ledger_for_transport",
    "verify_handoff",
    "verify_spawn_brief",
]
