"""Evolution-memory regression axis (issue #185; CRSI/SIM, arXiv 2611.18856).
Deterministic offline oracle over the evolution log — no live store, no
LLM, same discipline as memory_bench: the gate fails the build, never
silently passes. A case seeds EvolutionMemory with records from two
components, renders summarize_for() for one, and probes the summary:
decisive rationales must surface (must_surface, token containment), and
lessons from other components must never leak in (must_not_surface).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fusion_harness.evolution_memory import EvolutionMemory, EvolutionRecord

logger = logging.getLogger(__name__)


def evaluate_summary(summary: str, probes: Dict[str, List[str]]) -> Dict[str, Any]:
    """Score one rendered summary against surface/leak probes. Token
    containment, order-free, case-insensitive — same rule as memory_bench."""
    lowered = summary.lower()
    missed = []
    for probe in probes.get("must_surface", []):
        words = [w for w in probe.lower().split() if len(w) >= 2]
        if words and not all(w in lowered for w in words):
            missed.append(probe)
    leaked = []
    for probe in probes.get("must_not_surface", []):
        words = [w for w in probe.lower().split() if len(w) >= 2]
        if words and all(w in lowered for w in words):
            leaked.append(probe)
    return {"missed": missed, "leaked": leaked, "passed": not missed and not leaked}


def run_evolution_suite(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Each case: {"name", "component", "records": [record dicts],
    "probes": {"must_surface": [str], "must_not_surface": [str]}}.
    Records append to a fresh in-memory EvolutionMemory (append-only,
    verdict-gated by construction), then summarize_for(component) is
    scored against the probes."""
    results = []
    for case in cases:
        mem = EvolutionMemory()
        for data in case.get("records", []):
            mem.append(EvolutionRecord(**data))
        summary = mem.summarize_for(case["component"])
        verdict = evaluate_summary(summary, case.get("probes", {}))
        result = {
            "name": case["name"],
            "passed": verdict["passed"],
            "missed": verdict["missed"],
            "leaked": verdict["leaked"],
            "records": len(mem),
        }
        logger.info(
            "evolution-bench case=%s passed=%s missed=%d leaked=%d",
            result["name"],
            result["passed"],
            len(result["missed"]),
            len(result["leaked"]),
        )
        results.append(result)
    failed = [r["name"] for r in results if not r["passed"]]
    return {"cases": results, "failed": failed, "passed": not failed}


def assert_evolution_gate(report: Dict[str, Any]) -> None:
    """CI gate: any probe failure names the offending case."""
    bad = []
    for r in report["cases"]:
        if not r["passed"]:
            bad.append(
                "%s (missed: %s; leaked: %s)"
                % (r["name"], "; ".join(r["missed"]) or "-", "; ".join(r["leaked"]) or "-")
            )
    if bad:
        raise EvalGateError("evolution-bench gate failed: " + "; ".join(bad))


class EvalGateError(AssertionError):
    """Evolution gate failed: lessons leaked or decisive rationale lost."""


__all__ = [
    "EvalGateError",
    "assert_evolution_gate",
    "evaluate_summary",
    "run_evolution_suite",
]
