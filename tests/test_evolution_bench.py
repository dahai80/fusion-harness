"""Evolution-memory regression axis (issue #185; CRSI/SIM, arXiv 2611.18856).
Deterministic offline oracle over the evolution log: summarize_for must
surface the decisive rationale per component and never leak other
components' lessons. Same discipline as memory_bench: gate fails the
build, never silently passes."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

import pytest

from evolution_bench import (  # noqa: E402
    EvalGateError,
    assert_evolution_gate,
    evaluate_summary,
    run_evolution_suite,
)


def _rec(**kw):
    base = dict(
        component="prompt:summarizer",
        rationale="interceptor deletes stale results too early",
        predicted_outcomes=["summary keeps pin section"],
        observed_outcomes=["recall 0.7 to 0.9"],
        verdict="keep",
        eval_context={"model": "glm-5", "dataset": "reviewer-pack-2"},
        provenance="agent-studio/prompt_evolution",
    )
    base.update(kw)
    return base


def _case(name="decisive-rationale", **overrides):
    case = {
        "name": name,
        "component": "prompt:summarizer",
        "records": [
            _rec(),
            _rec(
                rationale="overfit: smuggled gold answers into few-shot",
                verdict="rollback",
            ),
            _rec(
                component="tool:search",
                rationale="search timeout retry storm floods queue",
                verdict="rollback",
            ),
        ],
        "probes": {
            "must_surface": [
                "interceptor deletes stale results too early",
                "rollback",
            ],
            "must_not_surface": ["search timeout retry storm floods queue"],
        },
    }
    case.update(overrides)
    return case


def test_decisive_rationale_surfaces():
    report = run_evolution_suite([_case()])
    assert report["passed"], report["failed"]


def test_cross_component_lesson_not_leaked():
    report = run_evolution_suite([_case()])
    assert report["cases"][0]["leaked"] == []


def test_leaking_summary_fails_probes():
    bad = (
        "evolution lessons for prompt:summarizer (2 records):\n"
        "- [keep] interceptor deletes stale results too early\n"
        "- [rollback] search timeout retry storm floods queue"
    )
    probes = {"must_surface": ["interceptor"], "must_not_surface": ["retry storm"]}
    result = evaluate_summary(bad, probes)
    assert not result["passed"]
    assert result["leaked"] == ["retry storm"]


def test_empty_summary_misses_surface_probe():
    result = evaluate_summary("", {"must_surface": ["interceptor deletes"], "must_not_surface": []})
    assert not result["passed"]
    assert result["missed"] == ["interceptor deletes"]


def test_suite_aggregates_failures():
    good = _case("good-case")
    bad = _case(
        "bad-case",
        records=[_rec(component="tool:search", rationale="unrelated other component lesson")],
        probes={"must_surface": ["interceptor deletes stale"], "must_not_surface": []},
    )
    report = run_evolution_suite([good, bad])
    assert report["failed"] == ["bad-case"]
    assert not report["passed"]


def test_gate_raises_with_case_name():
    report = {"cases": [{"name": "boom", "passed": False, "missed": ["x"], "leaked": []}], "failed": ["boom"], "passed": False}
    with pytest.raises(EvalGateError) as exc:
        assert_evolution_gate(report)
    assert "boom" in str(exc.value)


def test_gate_silent_on_green_report():
    report = {"cases": [{"name": "ok", "passed": True, "missed": [], "leaked": []}], "failed": [], "passed": True}
    assert_evolution_gate(report)
