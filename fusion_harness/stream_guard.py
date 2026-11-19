"""Stream loop detection + output-limit fallback signal (issue #95;
PydanticAI DetectLoops #12222 / ModelFallback #12407).

LoopDetector watches the three stream surfaces (thinking, text,
tool-call args) for repeated content and stops the run before the loop
burns budget — a loop IS a budget leak. Sensitivity is configurable via
the repeat threshold and window size.

ModelFallback: on output-token-limit truncation the funnel emits a typed
`model_fallback_required` event — the host decides whether to switch
models (complements json_repair.parse_repair: repair handles malformed
JSON, fallback handles the call that never finished). The kernel never
calls a provider itself.

Zero model calls, pure stdlib.
"""

import hashlib
import logging
from collections import deque
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SURFACES = ("thinking", "text", "tool_args")


class LoopDetector:
    def __init__(self, max_repeats: int = 3, window: int = 8, min_content_len: int = 8) -> None:
        if max_repeats < 2:
            raise ValueError("max_repeats must be >= 2")
        self.max_repeats = max_repeats
        self.window = window
        self.min_content_len = min_content_len
        self._history: Dict[str, deque] = {s: deque(maxlen=window) for s in SURFACES}
        self.loops_detected = 0

    @staticmethod
    def _key(content: Any) -> str:
        blob = content if isinstance(content, str) else repr(content)
        return hashlib.sha1(blob.encode("utf-8", errors="replace")).hexdigest()

    def observe(self, surface: str, content: Any) -> Optional[Dict[str, Any]]:
        """Feed one streamed chunk. Returns a loop event dict when the
        same content repeated max_repeats times consecutively on the
        surface, else None. Unknown surfaces are rejected loudly — the
        three-surface contract is the point."""
        if surface not in SURFACES:
            raise ValueError(
                "unknown loop surface: %s (allowed: %s)" % (surface, "|".join(SURFACES))
            )
        blob = content if isinstance(content, str) else repr(content)
        if len(blob.strip()) < self.min_content_len:
            return None
        key = self._key(blob)
        history = self._history[surface]
        history.append(key)
        repeats = 0
        for item in reversed(history):
            if item == key:
                repeats += 1
            else:
                break
        if repeats < self.max_repeats:
            return None
        self.loops_detected += 1
        event = {
            "surface": surface,
            "repeats": repeats,
            "content_hash": key,
            "threshold": self.max_repeats,
        }
        logger.warning(
            "loop detected surface=%s repeats=%d threshold=%d", surface, repeats, self.max_repeats
        )
        return event

    def reset(self, surface: Optional[str] = None) -> None:
        targets = [surface] if surface else SURFACES
        for s in targets:
            self._history[s].clear()


__all__ = ["SURFACES", "LoopDetector"]
