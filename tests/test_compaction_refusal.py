"""Tests for issue #88: compaction refusal/empty-output fallback branch.

A model refusal or empty summary on the compaction call must retry once
with a reworded prompt, then degrade to the deterministic extractor — the
failed summary never propagates into compaction state. Every branch is
counted in failure_stats for the Compact-Bench gate.
"""

import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

from compact_bench import load_fixtures, run_suite  # noqa: E402
from fusion_harness.compactor import CompactionConfig, Compactor  # noqa: E402


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubGateway:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.prompts = []

    async def chat(self, messages=None, **kwargs):
        self.prompts.append(messages[0]["content"])
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)


def _compactor(gateway):
    config = CompactionConfig(
        context_window=1400,
        warning_buffer=1000,
        error_buffer=300,
        manual_buffer=100,
        keep_recent_rounds=3,
    )
    return Compactor(config=config, llm_gateway=gateway)


def _msgs():
    msgs = [
        {"role": "system", "content": "You are a coding agent. Constraint: use ruff."},
        {"role": "user", "content": "Fix the bug in main.py. TODO: add tests"},
    ]
    for i in range(10):
        msgs.append(
            {
                "role": "assistant",
                "content": (
                    "edited module_%d.py at src/pkg/sub/dir, adjusted handler_%d "
                    "and reran the full suite locally; step %d complete. "
                    "Diff touched lines 40-120: replaced the retry loop with a "
                    "bounded backoff, added a logging statement per attempt, "
                    "and removed the deprecated import from utils_compat." % (i, i, i)
                ),
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": (
                    "continue task %d: next run the linter on module_%d and update "
                    "the changelog entry for handler_%d, then verify the release "
                    "notes section renders and the docstring lint passes" % (i, i, i)
                ),
            }
        )
    return msgs


def test_refusal_detection_patterns():
    cases = [
        "I can't help with that request.",
        "I cannot assist with distillation of this conversation.",
        "I won't summarize this content.",
        "I'm unable to comply.",
        "That would be against my guidelines.",
    ]
    for text in cases:
        assert Compactor._classify_summary_failure(text) == "refusals", text
    assert Compactor._classify_summary_failure("") == "empties"
    assert Compactor._classify_summary_failure("- fix main.py\n- add tests") == ""


def test_refusal_retries_reworded_then_falls_back():
    gateway = _StubGateway(["I cannot assist with that request.", ""])
    compactor = _compactor(gateway)
    out = compactor.compact(_msgs(), "warning")
    assert len(gateway.prompts) == 2
    assert "logged transcript" in gateway.prompts[1]
    assert "Summarize this agent conversation" not in gateway.prompts[1]
    assert out, "degraded fallback must still produce compacted state"
    stats = compactor.failure_stats
    assert stats["llm_calls"] == 1
    assert stats["refusals"] == 1
    assert stats["retries"] == 1
    assert stats["empties"] == 1
    assert stats["fallbacks"] == 1
    assert compactor.failure_rate() == 1.0


def test_good_summary_no_failure():
    gateway = _StubGateway(["- decision: keep main.py\n- TODO: tests"])
    compactor = _compactor(gateway)
    compactor.compact(_msgs(), "warning")
    stats = compactor.failure_stats
    assert stats["llm_calls"] == 1
    assert stats["fallbacks"] == 0
    assert compactor.failure_rate() == 0.0


def test_gateway_exception_falls_back():
    gateway = _StubGateway([RuntimeError("boom")])
    compactor = _compactor(gateway)
    out = compactor.compact(_msgs(), "warning")
    assert out
    assert compactor.failure_stats["fallbacks"] == 1
    assert compactor.failure_stats["refusals"] == 0


def test_fallback_emits_lifecycle_event():
    gateway = _StubGateway(["I can't do that.", "I won't do that."])
    from fusion_harness.funnels import LifecycleBus

    bus = LifecycleBus()
    compactor = _compactor(gateway)
    compactor.lifecycle_bus = bus
    compactor.compact(_msgs(), "warning")
    events = [e.event for e in bus.replay()]
    assert "compaction_fallback" in events


def test_bench_reports_failure_rate_for_refusing_gateway():
    gateway = _StubGateway(["I cannot assist with distilling this conversation."])
    compactor = _compactor(gateway)
    report = run_suite(compactor, load_fixtures())
    assert report["compaction_failure_rate"] > 0.0
    assert report["compaction_failures"]["refusals"] >= 1
    # Degraded deterministic fallback preserves pinned rules — the gate
    # stays green on the refusal branch itself.
    assert report["passed"], report["failed"]


def test_bench_report_no_llm_calls_rate_zero():
    compactor = _compactor(None)
    report = run_suite(compactor, load_fixtures())
    assert report["compaction_failure_rate"] == 0.0
    assert report["compaction_failures"]["llm_calls"] == 0
    assert report["compaction_failures"]["fallbacks"] == 0
