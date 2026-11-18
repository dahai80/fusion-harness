"""CI gate for the Compact-Bench-style A/B eval suite (issue #72): the
suite must be green on the current compactor, deterministic across runs,
and the seeded known-bad summarizer must FAIL the gate (proving the gate
can fail). Runs on every compactor change."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

from compact_bench import (  # noqa: E402
    BadSummarizer,
    EvalGateError,
    assert_gate,
    load_fixtures,
    run_suite,
)
from fusion_harness.compactor import CompactionConfig, Compactor  # noqa: E402


def _bench_compactor():
    # Small windows force real compaction on the fixture transcripts.
    config = CompactionConfig(
        context_window=1400,
        warning_buffer=400,
        error_buffer=300,
        manual_buffer=100,
        keep_recent_rounds=3,
        clear_tool_results=True,
    )
    return Compactor(config=config)


def test_gate_green_on_current_compactor():
    report = run_suite(_bench_compactor(), load_fixtures())
    assert report["passed"], report["failed"]
    assert_gate(report)
    for r in report["fixtures"]:
        assert r["variant_a"]["success"] is True
        assert r["variant_b"]["success"] is True
        assert r["rule_recall"] >= 1.0


def test_suite_deterministic():
    a = run_suite(_bench_compactor(), load_fixtures())
    b = run_suite(_bench_compactor(), load_fixtures())
    assert a == b


def test_tokens_saved_nonnegative_and_positive_somewhere():
    report = run_suite(_bench_compactor(), load_fixtures())
    assert all(r["tokens_saved"] >= 0 for r in report["fixtures"])
    assert any(r["tokens_saved"] > 0 for r in report["fixtures"])


def test_seeded_bad_summarizer_fails_gate():
    report = run_suite(BadSummarizer(), load_fixtures())
    assert not report["passed"]
    assert report["failed"]
    try:
        assert_gate(report)
        raise AssertionError("gate must fail for the seeded bad summarizer")
    except EvalGateError:
        pass


def test_bad_summarizer_loses_rules_but_gate_names_them():
    report = run_suite(BadSummarizer(), load_fixtures())
    dropped = [r for r in report["fixtures"] if r["rule_recall"] < 1.0]
    assert dropped, "seeded summarizer must drop at least one pinned rule"
    try:
        assert_gate(report)
    except EvalGateError as exc:
        assert dropped[0]["name"] in str(exc)
        assert dropped[0]["variant_b"]["missing_rules"]
