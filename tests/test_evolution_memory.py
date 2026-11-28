"""Issue #185 (proposed): evolution memory — SIM-shaped evolution records
(arXiv 2611.18856). Gate: records land only after a verdict; summarize_for
surfaces decisive rationales without leaking other components."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

from fusion_harness.evolution_memory import (  # noqa: E402
    EvolutionMemory,
    EvolutionMemoryError,
    EvolutionRecord,
)


def _rec(**kw):
    base = dict(
        component="prompt:summarizer",
        rationale="interceptor deletes stale results too early",
        predicted_outcomes=["summary keeps pin section"],
        observed_outcomes=["recall 0.7 -> 0.9"],
        verdict="keep",
        eval_context={"model": "glm-5", "dataset": "reviewer-pack-2", "budget": "50k"},
        provenance="agent-studio/prompt_evolution",
    )
    base.update(kw)
    return EvolutionRecord(**base)


def test_append_requires_verdict():
    mem = EvolutionMemory()
    try:
        mem.append(_rec(verdict=""))
    except EvolutionMemoryError as exc:
        assert "verdict" in str(exc)
    else:
        raise AssertionError("append without verdict must raise")


def test_append_requires_rationale_and_component():
    mem = EvolutionMemory()
    for drop in ("rationale", "component"):
        kw = {drop: ""}
        try:
            mem.append(_rec(**kw))
        except EvolutionMemoryError as exc:
            assert drop in str(exc)
        else:
            raise AssertionError("append without %s must raise" % drop)


def test_append_requires_sim_fields():
    mem = EvolutionMemory()
    try:
        mem.append(_rec(predicted_outcomes=[], observed_outcomes=[]))
    except EvolutionMemoryError as exc:
        assert "predicted" in str(exc) or "observed" in str(exc)
    else:
        raise AssertionError("record without predicted/observed outcomes must raise")


def test_auto_change_id_sequential():
    mem = EvolutionMemory()
    r1 = mem.append(_rec())
    r2 = mem.append(_rec())
    assert r1.change_id and r2.change_id and r1.change_id != r2.change_id


def test_query_filters_component_and_verdict():
    mem = EvolutionMemory()
    mem.append(_rec())
    mem.append(_rec(component="tool:search", rationale="search timeout retry storm"))
    mem.append(_rec(rationale="overfit: smuggled gold answers", verdict="rollback"))
    keeps = mem.query(component="prompt:summarizer", verdict="keep")
    assert len(keeps) == 1
    rollbacks = mem.query(verdict="rollback")
    assert len(rollbacks) == 1 and "smuggled" in rollbacks[0].rationale


def test_summarize_surfaces_decisive_rationale():
    mem = EvolutionMemory()
    mem.append(_rec(rationale="early clearing dropped the pin section"))
    summary = mem.summarize_for("prompt:summarizer")
    words = [w for w in "early clearing dropped pin section".split()]
    assert all(w in summary.lower() for w in words)


def test_summarize_excludes_other_components():
    mem = EvolutionMemory()
    mem.append(_rec(rationale="search timeout retry storm", component="tool:search"))
    assert mem.summarize_for("prompt:summarizer") == ""


def test_summarize_empty_returns_empty_string():
    mem = EvolutionMemory()
    assert mem.summarize_for("prompt:summarizer") == ""


def test_rollback_record_kept_as_lesson():
    mem = EvolutionMemory()
    mem.append(_rec(rationale="overfit: case-specific hacks", verdict="rollback"))
    summary = mem.summarize_for("prompt:summarizer")
    assert "overfit" in summary and "rollback" in summary.lower()


def test_jsonl_roundtrip_and_corrupt_line_skip():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "evo.jsonl")
        mem = EvolutionMemory(path=path)
        mem.append(_rec(rationale="early clearing dropped pin section"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{corrupt json\n")
        mem2 = EvolutionMemory(path=path)
        assert len(mem2.query(component="prompt:summarizer")) == 1
        assert "early clearing" in mem2.summarize_for("prompt:summarizer")


def test_counts_telemetry():
    mem = EvolutionMemory()
    mem.append(_rec())
    mem.append(_rec(verdict="rollback"))
    counts = mem.counts()
    assert counts["keep"] == 1 and counts["rollback"] == 1
