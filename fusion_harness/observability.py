"""Observability loop (G4, v1.0 product definition; issue #93 consumer).

Closes the loop between kernel telemetry and the (agent, step, mode)
failure report: lifecycle/funnel events land as normalized records, the
same records feed a live meter snapshot AND the FailureAnalyzer
post-hoc attribution, so consumers get one report instead of scraping
funnel internals. Zero model calls — the pinpointer stays injectable.

Report shape (json-able, cowork / agent-studio run views):
    {
      "agent": ..., "run_id": ...,
      "snapshot": {calls, failures, budget_spent_tokens, budget_exceeded, events},
      "attribution": {runs: {...}, mode_counts, cluster_counts},
    }
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .failure_attribution import FailureAnalyzer

logger = logging.getLogger(__name__)


@dataclass
class ObservabilityRecord:
    """Normalized event record matching the FailureAnalyzer contract
    (run_id / ts / node_id / event / payload duck-typing)."""

    ts: float
    run_id: str
    node_id: str
    event: str
    payload: Dict[str, Any] = field(default_factory=dict)
    args: Any = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "event": self.event,
            "payload": self.payload,
        }


class ObservabilityLoop:
    """Per-run collector: records -> meter snapshot + failure attribution.

    Wire it two ways: push events via record()/observe_bus(), or let the
    sidecar feed pre_llm/post_llm/tool_admit outcomes. budget_ref and
    llm_funnel are optional live telemetry sources for snapshot().
    """

    def __init__(
        self,
        agent_id: str = "",
        run_id: str = "",
        budget_ref=None,
        llm_funnel=None,
        analyzer: Optional[FailureAnalyzer] = None,
        max_records: int = 5000,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.budget_ref = budget_ref
        self.llm_funnel = llm_funnel
        self.analyzer = analyzer or FailureAnalyzer()
        self.records: List[ObservabilityRecord] = []
        self.max_records = max_records
        self.counts: Dict[str, int] = {}

    def record(
        self,
        event: str,
        node_id: str = "",
        payload: Optional[dict] = None,
        args: Any = None,
        run_id: str = "",
        ts: Optional[float] = None,
    ) -> ObservabilityRecord:
        rec = ObservabilityRecord(
            ts=time.time() if ts is None else ts,
            run_id=run_id or self.run_id or "unknown",
            node_id=node_id,
            event=event,
            payload=payload or {},
            args=args,
        )
        self.records.append(rec)
        if len(self.records) > self.max_records:
            # Bounded ring: drop oldest, keep the report honest about it.
            self.records = self.records[-self.max_records :]
            self.counts["records_dropped"] = self.counts.get("records_dropped", 0) + 1
        self.counts[event] = self.counts.get(event, 0) + 1
        return rec

    def observe_bus(self, bus) -> None:
        """Subscribe to a LifecycleBus; every emitted event becomes a record."""

        def _sink(ev) -> None:
            try:
                self.record(
                    event=getattr(ev, "event", ""),
                    node_id=str(getattr(ev, "payload", {}).get("node", "")),
                    payload=dict(getattr(ev, "payload", {}) or {}),
                    run_id=getattr(ev, "session_id", "") or self.run_id,
                    ts=getattr(ev, "ts", None),
                )
            except Exception as e:  # notify path: never break the emitter
                logger.warning("observability bus sink error: %s", e)

        bus.subscribe(_sink)

    def snapshot(self) -> dict:
        """Live meter view: funnel meter + budget + event counters."""
        snap: Dict[str, Any] = {
            "agent": self.agent_id,
            "run_id": self.run_id,
            "events": dict(self.counts),
            "records": len(self.records),
        }
        if self.llm_funnel is not None:
            try:
                snap["llm"] = self.llm_funnel.meter()
            except Exception as e:
                logger.warning("llm meter read failed: %s", e)
        budget = self.budget_ref
        snap["budget_spent_tokens"] = getattr(budget, "spent_tokens", None)
        try:
            snap["budget_exceeded"] = bool(budget.is_exceeded()) if budget is not None else False
        except TypeError:
            snap["budget_exceeded"] = bool(budget.is_exceeded()) if budget is not None else False
        return snap

    def attribution(self) -> dict:
        """(agent, step, mode) verdicts over the recorded trajectory."""
        try:
            return self.analyzer.analyze(self.records)
        except Exception as e:
            logger.warning("failure attribution failed: %s", e)
            return {"runs": {}, "mode_counts": {}, "cluster_counts": {}, "error": str(e)}

    def report(self) -> dict:
        """The G4 loop deliverable: snapshot + attribution in one report."""
        out = {"snapshot": self.snapshot(), "attribution": self.attribution()}
        if self.counts.get("records_dropped"):
            out["snapshot"]["records_dropped"] = self.counts["records_dropped"]
        return out


__all__ = ["ObservabilityLoop", "ObservabilityRecord"]
