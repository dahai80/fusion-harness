"""Untrusted-data discipline for tool results (issue #147, S2).

Indirect prompt injection via tool results is the top agent threat class
(AgentDojo arXiv:2406.13352, InjecAgent arXiv:2403.02691). This module is
the deterministic detection half: a pure pattern table over tool-returned
text, no model calls anywhere (Rule 5). Matches feed two consumers — the
ToolFunnel's untrusted-origin flag (context labeling) and the escalation
invariant (destructive calls after exposure need approval, S3).
"""

import re
from typing import List

INJECTION_PATTERNS = {
    "ignore_instructions": r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    "disregard_instructions": r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions",
    "fake_role_tag": r"</?\s*(system|assistant)\s*>",
    "fake_system_prompt": r"system\s*(prompt|message)\s*[:=]",
    "identity_override": r"you\s+are\s+now\s+(a|an|the)\s",
}

_COMPILED = [
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in INJECTION_PATTERNS.items()
]

__all__ = ["INJECTION_PATTERNS", "scan_untrusted"]


def scan_untrusted(text: str) -> List[str]:
    """Deterministic injection-pattern scan of tool-returned text. Returns
    the matched pattern labels (empty = clean). Pure regex, no model call."""
    if not text:
        return []
    return [label for label, pattern in _COMPILED if pattern.search(text)]
