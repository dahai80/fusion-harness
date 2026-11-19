"""Issue #100: memory_bench four-competency regression axis — the gate
must pass on a healthy consolidator and fail on seeded damage."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

from memory_bench import (  # noqa: E402
    EvalGateError,
    assert_memory_gate,
    run_memory_suite,
)

HEALTHY = [
    {
        "name": "pin-survives-roundtrips",
        "facts": ["release day is friday"],
        "deltas": ["deploy on friday at noon"],
        "probes": {"must_survive": ["release day is friday"], "must_not_retrieve": []},
    },
    {
        "name": "midrun-learning",
        "facts": ["team uses rust for tools"],
        "deltas": ["operator prefers dark mode always"],
        "probes": {"must_survive": ["team uses rust for tools"], "must_not_retrieve": []},
    },
    {
        "name": "retraction-selective-forgetting",
        "facts": ["deploy on friday", "cats own the office"],
        "deltas": ["retract the deploy on friday note"],
        "probes": {
            "must_survive": ["cats own the office"],
            "must_not_retrieve": ["deploy on friday"],
        },
    },
]


def test_healthy_suite_passes_gate():
    report = run_memory_suite(HEALTHY)
    assert report["passed"], report["failed"]
    assert_memory_gate(report)


def test_over_forgetting_fails_gate():
    # Seeded damage: an aggressive gate that tombstones everything must
    # fail the selective-forgetting competency.
    import memory_bench
    from fusion_harness.memory_consolidation import MemoryConsolidator

    class BullyConsolidator(MemoryConsolidator):
        def consolidate(self, candidate, scope="memory"):
            for n in self.retrieve_neighbors(candidate):
                self.sink.store(
                    key=n.key,
                    payload=n.payload,
                    metadata={**n.metadata, "tombstoned": True},
                )
                self.stats["deletes"] += 1
            return super().consolidate(candidate, scope)

    cases = [
        {
            "name": "over-forgetting",
            "facts": ["release day is friday", "cats own the office"],
            "deltas": ["deploy on friday at noon"],
            "probes": {"must_survive": ["cats own the office"], "must_not_retrieve": []},
        }
    ]
    orig = memory_bench.MemoryConsolidator
    memory_bench.MemoryConsolidator = BullyConsolidator
    try:
        report = run_memory_suite(cases)
    finally:
        memory_bench.MemoryConsolidator = orig
    assert not report["passed"]
    try:
        assert_memory_gate(report)
        raise AssertionError("gate must fail on over-forgetting")
    except EvalGateError as exc:
        assert "over-forgetting" in str(exc)


def test_retracted_fact_not_retrievable():
    report = run_memory_suite(HEALTHY)
    case = report["cases"][2]
    assert case["selective_forgetting"]
    assert case["leaked_forgotten"] == []
