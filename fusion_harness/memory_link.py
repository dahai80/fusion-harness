"""Memory binding protocols (plan P3-2, gap G9).

Memory is state, not transcript replay. The kernel defines sink/source
protocols; fusion-memory (or any store) implements them consumer-side.
Retrieval is just-in-time (injected at prompt-build), never preloaded.
No engine dependency — pure stdlib protocols + a composition bridge.
"""

import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from fusion_harness.artifact_scan import (
    ArtifactQuarantine,
    ScanReport,
    scan_artifact,
    stamp_untrusted,
)

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
        self,
        sink: Optional[MemorySink] = None,
        source: Optional[MemorySource] = None,
        quarantine: Optional[ArtifactQuarantine] = None,
        bus=None,
    ) -> None:
        self.sink = sink
        self.source = source
        # Issue #75: memory write gate + quarantine. Flagged writes are
        # stored stamped/quoted with origin and never injected; flagged
        # recalls are dropped before prompt assembly.
        self.quarantine = quarantine
        self.bus = bus

    def _quarantine_hit(self, report: ScanReport, payload: str, where: str) -> None:
        if self.quarantine is not None:
            self.quarantine.add(report, preview=payload)
        if self.bus is not None:
            try:
                self.bus.emit(
                    "artifact_scan",
                    "artifact_quarantined",
                    {
                        "where": where,
                        "source": report.source,
                        "kind": report.kind,
                        "rules": report.rule_names(),
                    },
                    session_id="",
                )
            except Exception as exc:
                logger.warning("artifact scan event emit failed: %s", exc)

    def persist_compaction(
        self, summary: str, session_id: str = "", scope: str = "compaction"
    ) -> bool:
        if self.sink is None or not summary:
            return False
        report = scan_artifact(summary, source=f"{scope}:{session_id}", kind="memory")
        if not report.clean:
            logger.warning(
                "memory write gated source=%s rules=%s",
                f"{scope}:{session_id}",
                ",".join(report.rule_names()),
            )
            self._quarantine_hit(report, summary, "memory_write")
            summary = stamp_untrusted(summary, f"{scope}:{session_id}", report.rule_names())
        try:
            metadata: Dict[str, Any] = {"scope": scope, "session_id": session_id}
            if not report.clean:
                metadata["quarantined"] = True
                metadata["origin"] = "untrusted"
                metadata["scan_rules"] = report.rule_names()
            self.sink.store(
                key=f"{scope}:{session_id}",
                payload=summary,
                metadata=metadata,
            )
            logger.info(
                "compaction summary persisted scope=%s session=%s chars=%d quarantined=%s",
                scope,
                session_id,
                len(summary),
                not report.clean,
            )
            return True
        except Exception as exc:
            logger.warning("memory sink store failed: %s", exc)
            return False

    def recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Just-in-time retrieval; failures return empty (recall is an upgrade,
        never a dependency — same discipline as the compactor gateway).
        Flagged items are quarantined and never returned (issue #75)."""
        if self.source is None:
            return []
        try:
            items = list(self.source.retrieve(query, limit=limit) or [])
        except Exception as exc:
            logger.warning("memory source retrieve failed: %s", exc)
            return []
        clean = []
        for item in items:
            payload = str(item.get("payload") or item)
            already = bool((item.get("metadata") or {}).get("quarantined"))
            report = scan_artifact(payload, source=str(item.get("key") or ""), kind="memory")
            if already or not report.clean:
                if not report.clean:
                    self._quarantine_hit(report, payload, "memory_recall")
                logger.warning("memory recall dropped flagged item key=%s", item.get("key"))
                continue
            clean.append(item)
        logger.info(
            "memory recall query_chars=%d items=%d kept=%d", len(query), len(items), len(clean)
        )
        return clean

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
