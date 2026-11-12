"""Memory binding protocols (plan P3-2, gap G9).

Memory is state, not transcript replay. The kernel defines sink/source
protocols; fusion-memory (or any store) implements them consumer-side.
Retrieval is just-in-time (injected at prompt-build), never preloaded.
No engine dependency — pure stdlib protocols + a composition bridge.
"""

import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MemorySink(Protocol):
    def store(self, key: str, payload: str, metadata: Optional[Dict[str, Any]] = None) -> None: ...


@runtime_checkable
class MemorySource(Protocol):
    def retrieve(self, query: str, limit: int = 5) -> List[Dict[str, Any]]: ...


class MemoryBridge:
    """Composition point: compaction summaries persist through a sink; new
    sessions pull relevant state back through a source at prompt-build time."""

    def __init__(
        self, sink: Optional[MemorySink] = None, source: Optional[MemorySource] = None
    ) -> None:
        self.sink = sink
        self.source = source

    def persist_compaction(
        self, summary: str, session_id: str = "", scope: str = "compaction"
    ) -> bool:
        if self.sink is None or not summary:
            return False
        try:
            self.sink.store(
                key=f"{scope}:{session_id}",
                payload=summary,
                metadata={"scope": scope, "session_id": session_id},
            )
            logger.info(
                "compaction summary persisted scope=%s session=%s chars=%d",
                scope,
                session_id,
                len(summary),
            )
            return True
        except Exception as exc:
            logger.warning("memory sink store failed: %s", exc)
            return False

    def recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Just-in-time retrieval; failures return empty (recall is an upgrade,
        never a dependency — same discipline as the compactor gateway)."""
        if self.source is None:
            return []
        try:
            items = self.source.retrieve(query, limit=limit)
            logger.info("memory recall query_chars=%d items=%d", len(query), len(items))
            return list(items or [])
        except Exception as exc:
            logger.warning("memory source retrieve failed: %s", exc)
            return []

    def inject_context(
        self, messages: List[Dict[str, Any]], query: str, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """Append recalled memory as a system segment after existing system
        messages (just-in-time, bounded). No-op when nothing recalls."""
        items = self.recall(query, limit=limit)
        if not items:
            return messages
        body = "\n".join(str(i.get("payload") or i) for i in items)
        segment = {"role": "system", "content": "[Recalled memory]\n" + body}
        insert_at = 0
        for idx, m in enumerate(messages):
            if m.get("role") == "system":
                insert_at = idx + 1
            else:
                break
        logger.info("memory injected items=%d at=%d", len(items), insert_at)
        return messages[:insert_at] + [segment] + messages[insert_at:]


__all__ = ["MemorySink", "MemorySource", "MemoryBridge"]
