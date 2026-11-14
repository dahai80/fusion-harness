"""KV-cache discipline layer (issue #29, P7).

KV-cache hit rate is the top production cost/latency metric for agent
workloads (Manus engineering blog 2025-07-18: input:output ~100:1, 10x
cached-vs-uncached price gap). Three kernel guards:

- Stable prefix: harness prompt builders must produce byte-identical
  prefixes across turns — no volatile content (timestamps, request ids)
  before cacheable content. `assert_stable_prefix` flags violations.
- Append-only context: context mutation prefers appends; rewrites happen
  only at explicit compaction boundaries. `check_append_only` verifies.
- Mask, don't remove: mid-task tool availability changes are expressed as
  masking flags on an UNCHANGED schema list, never by add/remove — dynamic
  tool-list mutation invalidates the KV prefix and induces schema
  violations.
"""

import logging
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)


def assert_stable_prefix(turn_prompts: Sequence[str]) -> List[int]:
    """Return indexes of turns whose prompt does not extend the first
    turn's prefix byte-identically (empty list = all stable)."""
    if not turn_prompts:
        return []
    first = turn_prompts[0]
    unstable = [
        i
        for i, prompt in enumerate(turn_prompts)
        if not prompt.startswith(first[: len(prompt)]) or len(prompt) < len(first)
    ]
    if unstable:
        logger.warning("unstable prompt prefix detected turns=%s", unstable)
    return unstable


def check_append_only(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]], compaction: bool = False
) -> bool:
    """True when `after` is `before` + appended messages. Rewrites are only
    permitted when compaction=True (explicit compaction boundary)."""
    if len(after) < len(before):
        if compaction:
            logger.info("context shrink allowed at compaction boundary")
            return True
        logger.warning("context shrank outside compaction boundary")
        return False
    prefix_ok = all(before[i] == after[i] for i in range(len(before)))
    if not prefix_ok:
        logger.warning("context prefix mutated outside compaction boundary")
    return prefix_ok


def mask_tools(tools: List[Dict[str, Any]], available: Sequence[str]) -> List[Dict[str, Any]]:
    """Express tool availability as a mask on an unchanged list: order and
    length are preserved (KV prefix stays valid); unavailable tools get
    x-fusion-masked=true instead of being removed."""
    available_set = set(available)
    masked = []
    for tool in tools:
        entry = dict(tool)
        name = entry.get("name") or entry.get("function", {}).get("name") or ""
        entry["x-fusion-masked"] = name not in available_set
        masked.append(entry)
    removed = sum(1 for t in masked if t["x-fusion-masked"])
    logger.info("tool mask applied total=%d masked=%d", len(masked), removed)
    return masked


class ToolAvailabilityState:
    """State-machine driven availability: transitions are explicit steps,
    masking is derived — the tool list itself is never mutated."""

    def __init__(self, tools: List[Dict[str, Any]], initial: Sequence[str]) -> None:
        self._tools = tools
        self._available = set(initial)
        self._history: List[set] = [set(initial)]

    def transition(self, available: Sequence[str]) -> None:
        self._available = set(available)
        self._history.append(set(available))
        logger.info(
            "tool availability transitioned available=%d history=%d",
            len(self._available),
            len(self._history),
        )

    def masked(self) -> List[Dict[str, Any]]:
        return mask_tools(self._tools, self._available)

    @property
    def available(self) -> set:
        return set(self._available)


__all__ = [
    "assert_stable_prefix",
    "check_append_only",
    "mask_tools",
    "ToolAvailabilityState",
]
