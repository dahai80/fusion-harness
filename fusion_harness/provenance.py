"""CaMeL-style provenance tags for tool results (issue #97).

Design: docs/design/provenance-tags-97.md. Every tool result gets an
O(1) metadata sidecar (no content scanning at runtime); downstream calls
whose arguments contain exact substrings of tainted outputs inherit the
union of taints; outbound calls to exfil-capable tools with tainted
arguments require host approval. The tracker is opt-in — an unwired
tracker means zero behavior change in the funnel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ORIGIN_USER_INPUT = "user_input"
ORIGIN_EXTERNAL = "external"
ORIGIN_DERIVED = "derived"

TAINT_EXTERNAL_CONTENT = "external_content"
TAINT_PRIVATE_DATA = "contains_private_data"

# Network-capable tools: tainted args flowing here can exfiltrate data.
EXFIL_CAPABLE = frozenset(
    {
        "fetch_url",
        "http_post",
        "web_search",
        "send_message",
        "upload_file",
        "net_request",
    }
)

# Bounded derivation memory: snippets kept per tainted output for
# exact-substring propagation (cheap, incomplete by design — hosts with
# a real dataflow map supply their own derivation instead).
_SNIPPET_LEN = 512
_MAX_RECORDED = 256


@dataclass(frozen=True)
class Provenance:
    """Immutable sidecar tag attached to a tool result at birth."""

    source_tool: str = ""
    origin: str = ORIGIN_EXTERNAL
    taints: frozenset = field(default_factory=frozenset)
    ts: float = field(default_factory=time.time)
    call_id: str = ""

    def to_dict(self) -> dict:
        return {
            "source_tool": self.source_tool,
            "origin": self.origin,
            "taints": sorted(self.taints),
            "ts": self.ts,
            "call_id": self.call_id,
        }

    def has_taint(self, taint: str) -> bool:
        return taint in self.taints


class ProvenanceTracker:
    """Tags tool results, propagates taint via exact-substring matches,
    and gates outbound calls to exfil-capable tools.

    Narrow-only by contract: the funnel consults the tracker AFTER the
    permission engine admitted the call, so a tracker verdict can deny or
    ask but never widen a permission deny.
    """

    def __init__(self, user_arg_keys: frozenset = frozenset()):
        # Arg keys whose values originate from typed user input.
        self.user_arg_keys = frozenset(user_arg_keys)
        self._recorded: list[tuple[tuple[str, ...], frozenset]] = []
        self.stats = {"tagged": 0, "propagated": 0, "ask": 0, "deny": 0}

    def tag(
        self,
        tool_name: str,
        args: dict | None = None,
        call_id: str = "",
    ) -> Provenance:
        """O(1): attach a sidecar tag from call metadata, no content scan."""
        args = args or {}
        user_touched = any(k in self.user_arg_keys for k in args)
        if user_touched:
            origin = ORIGIN_USER_INPUT
            taints: frozenset = frozenset()
        else:
            origin = ORIGIN_EXTERNAL
            taints = frozenset({TAINT_EXTERNAL_CONTENT})
        self.stats["tagged"] += 1
        return Provenance(
            source_tool=tool_name,
            origin=origin,
            taints=taints,
            call_id=call_id,
        )

    def observe_result(self, prov: Provenance, output: str) -> None:
        """Record snippets of a tainted output for later propagation.

        Stores only bounded head/tail snippets — never the full payload.
        """
        if not prov.taints or not output:
            return
        snippets = (output[:_SNIPPET_LEN], output[-_SNIPPET_LEN:])
        self._recorded.append((snippets, prov.taints))
        if len(self._recorded) > _MAX_RECORDED:
            self._recorded.pop(0)
            logger.debug("provenance derivation memory evicted oldest entry")

    def propagate(self, args: dict | None = None) -> frozenset:
        """Union of taints inherited by arguments carrying tainted text."""
        args = args or {}
        inherited: set = set()
        for value in args.values():
            if not isinstance(value, str) or not value:
                continue
            for snippets, taints in self._recorded:
                if any(s and s in value for s in snippets):
                    inherited |= taints
        if inherited:
            self.stats["propagated"] += 1
        return frozenset(inherited)

    def check_outbound(
        self, tool_name: str, args: dict | None = None
    ) -> tuple[str, str, frozenset]:
        """Policy check for an outbound call.

        Returns (verdict, reason, taints). Verdict is "allow", "ask" or
        "deny":
        - tainted args + exfil-capable tool -> "ask" (host approval)
        - derived args + high-risk tool -> "deny" (unknown-taint default)
        - otherwise "allow"
        """
        args = args or {}
        taints = self.propagate(args)
        derived = ORIGIN_DERIVED if taints else ""
        if taints and tool_name in EXFIL_CAPABLE:
            self.stats["ask"] += 1
            reason = "tainted args to exfil-capable tool require host approval"
            logger.info("provenance ask tool=%s taints=%s", tool_name, sorted(taints))
            return "ask", reason, taints
        if derived and tool_name in HIGH_RISK_TOOLS:
            self.stats["deny"] += 1
            reason = "derived (unknown-taint) args to high-risk tool denied by default"
            logger.warning("provenance deny tool=%s taints=%s", tool_name, sorted(taints))
            return "deny", reason, taints
        return "allow", "", taints


# Tools whose admission the funnel already guards via risk bands; a
# derived-arg call into one of them defaults to deny under the tracker.
HIGH_RISK_TOOLS = frozenset({"shell_exec", "apply_edit", "write_file", "delete_file"})
