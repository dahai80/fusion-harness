"""Token counting seam (#plan P1-2).

Replaces the chars/4 heuristic with an injectable TokenCounter protocol.
Default stays heuristic (stdlib-only installs); consumers inject a
model-accurate counter (tokenizer owned by the engine side). Nothing here
imports a tokenizer — pure stdlib.
"""

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class TokenCounter(Protocol):
    def count_text(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """Legacy chars/4 estimate — kept as stdlib fallback, unchanged behavior."""

    def count_text(self, text: str) -> int:
        return max(1, len(text or "") // 4)


class CJKAwareHeuristicTokenCounter:
    """Chars/4 with a CJK correction: CJK chars count ~1 token each.

    Bounded correction only — no tokenizer dependency. For mixed text,
    ASCII keeps the /4 ratio, CJK runs are counted at 1.0 per char.
    """

    def count_text(self, text: str) -> int:
        text = text or ""
        if not text:
            return 1
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        ascii_chars = len(text) - cjk
        estimate = ascii_chars // 4 + cjk
        return max(1, estimate)


def count_messages(messages: list[dict], counter: TokenCounter) -> int:
    """Count a message list: content + tool-call name/arguments per message."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += counter.count_text(content)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += counter.count_text(str(fn.get("name", "")))
            total += counter.count_text(str(fn.get("arguments", "")))
    return max(1, total)


__all__ = [
    "TokenCounter",
    "HeuristicTokenCounter",
    "CJKAwareHeuristicTokenCounter",
    "count_messages",
]
