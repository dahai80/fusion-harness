"""Memory-consolidation regression axis (issue #100; MemoryAgentBench,
arXiv 2507.05257). Four competencies as deterministic offline checks over
a recorded memory corpus — no live store, no LLM. Same discipline as
compact_bench (#72): the gate fails the build, never silently passes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fusion_harness.memory_consolidation import MemoryConsolidator, is_retrievable

logger = logging.getLogger(__name__)


class InMemorySink:
    """Deterministic stand-in for the fusion-memory store offline."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    def store(self, key: str, payload: str, metadata: Dict[str, Any] | None = None) -> None:
        self.items[key] = {"key": key, "payload": payload, "metadata": dict(metadata or {})}

    def retrieve(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        from fusion_harness.memory_consolidation import similarity

        scored = [
            {**item, "score": similarity(query, item["payload"])}
            for item in self.items.values()
            if is_retrievable(item)
        ]
        scored.sort(key=lambda x: -x["score"])
        return scored[:limit]

    def live_payloads(self) -> List[str]:
        return [i["payload"] for i in self.items.values() if is_retrievable(i)]


class _Bus:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, domain: str, event: str, payload: dict, session_id: str = "") -> None:
        self.events.append({"event": event, **payload})


def run_memory_suite(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Each case: {"name", "facts": [str], "deltas": [str], "probes":
    {"must_survive": [str], "must_not_retrieve": [str]}}. Facts are
    written first, deltas run through the consolidation gate, then the
    probes score retrieval against the live store."""
    results = []
    for case in cases:
        sink = InMemorySink()
        bus = _Bus()
        cons = MemoryConsolidator(sink=sink, source=sink, lifecycle_bus=bus)
        for fact in case.get("facts", []):
            sink.store(
                key="seed:%s" % abs(hash(fact)), payload=fact, metadata={"origin": "user_input"}
            )
        for delta in case.get("deltas", []):
            cons.consolidate(delta)
        live = sink.live_payloads()

        def present(text: str) -> bool:
            # A probe survives if some live payload still carries its
            # distinctive words (all of them, order-free).
            words = [w for w in text.lower().split() if len(w) >= 2]
            return any(all(w in p.lower() for w in words) for p in live) if words else False

        missed_survivors = [
            p for p in case.get("probes", {}).get("must_survive", []) if not present(p)
        ]
        leaked_forgotten = [
            p for p in case.get("probes", {}).get("must_not_retrieve", []) if present(p)
        ]
        over_forgetting = bool(leaked_forgotten)
        learning_deltas = [d for d in case.get("deltas", []) if not _RETRACT_HINT.search(d or "")]
        result = {
            "name": case["name"],
            "accurate_retrieval": not missed_survivors,
            "selective_forgetting": not over_forgetting,
            "test_time_learning": present(learning_deltas[-1]) if learning_deltas else True,
            "long_range_linked": _long_range_ok(case, live),
            "missed_survivors": missed_survivors,
            "leaked_forgotten": leaked_forgotten,
            "events": len(bus.events),
        }
        logger.info(
            "memory-bench case=%s retrieval=%s forgetting=%s learning=%s range=%s",
            result["name"],
            result["accurate_retrieval"],
            result["selective_forgetting"],
            result["test_time_learning"],
            result["long_range_linked"],
        )
        results.append(result)
    failed = [r["name"] for r in results if not _case_passes(r)]
    return {"cases": results, "failed": failed, "passed": not failed}


def _long_range_ok(case: Dict[str, Any], live: List[str]) -> bool:
    """Multi-hop facts stay linked: seeded facts that were NOT retracted
    must still be present after the delta round."""
    retracted = [d for d in case.get("deltas", []) if _RETRACT_HINT.search(d or "")]
    for fact in case.get("facts", []):
        if any(_containment_at_least(fact, d, 0.5) for d in retracted):
            continue
        words = [w for w in fact.lower().split() if len(w) >= 2]
        if words and not any(all(w in p.lower() for w in words) for p in live):
            return False
    return True


_RETRACT_HINT = __import__("re").compile(r"retract|forget|disregard|withdraw", __import__("re").I)


def _containment_at_least(a: str, b: str, floor: float) -> bool:
    from fusion_harness.memory_consolidation import containment

    return containment(a, b) >= floor


def _case_passes(result: Dict[str, Any]) -> bool:
    return (
        result["accurate_retrieval"]
        and result["selective_forgetting"]
        and result["test_time_learning"]
        and result["long_range_linked"]
    )


def assert_memory_gate(report: Dict[str, Any]) -> None:
    """CI gate: any competency failure names the offending case."""
    bad = []
    for r in report["cases"]:
        if not _case_passes(r):
            bad.append(
                "%s (retrieval=%s forgetting=%s learning=%s range=%s missed=%s leaked=%s)"
                % (
                    r["name"],
                    r["accurate_retrieval"],
                    r["selective_forgetting"],
                    r["test_time_learning"],
                    r["long_range_linked"],
                    r["missed_survivors"],
                    r["leaked_forgotten"],
                )
            )
    if bad:
        raise EvalGateError("memory-bench gate failed: " + "; ".join(bad))


class EvalGateError(AssertionError):
    """Memory gate failed: consolidation damaged retrieval."""


__all__ = [
    "EvalGateError",
    "InMemorySink",
    "assert_memory_gate",
    "run_memory_suite",
]
