"""Tests for issue #91: budget funnel hardening — billing is unconditional
on parse success, parse-error vs truncation are separately observable, and
the read-only meter view exposes funnel state without scraping internals.
"""

import pytest

from fusion_harness.funnels import LLMFunnel


class _Budget:
    def __init__(self) -> None:
        self.spent_tokens = 0

    def is_exceeded(self, graph_id=""):
        return False

    def record_usage(self, prompt, completion, graph_id=""):
        self.spent_tokens += prompt + completion


class _Ctx:
    session_id = "s1"
    graph_id = "g1"


@pytest.mark.asyncio
async def test_parse_failed_call_still_billed():
    budget = _Budget()
    funnel = LLMFunnel(budget_ref=budget)
    ok, reason, _ = await funnel.pre_llm(_Ctx(), "node1")
    assert ok
    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    funnel.post_llm(_Ctx(), "node1", usage=usage, parse_error=True)
    assert budget.spent_tokens == 150, "parse failure must not skip billing"
    assert funnel.calls == 1


@pytest.mark.asyncio
async def test_parse_error_counted_not_truncation():
    funnel = LLMFunnel()
    await funnel.pre_llm(_Ctx(), "n")
    funnel.post_llm(_Ctx(), "n", usage={}, parse_error=True)
    assert funnel.failures["parse_error"] == 1
    assert funnel.failures["truncation"] == 0
    assert funnel.meter()["failures"]["parse_error"] == 1


@pytest.mark.asyncio
async def test_truncation_counted_not_parse_error():
    funnel = LLMFunnel()
    await funnel.pre_llm(_Ctx(), "n")
    funnel.post_llm(_Ctx(), "n", usage={}, finish_reason="length")
    assert funnel.failures["truncation"] == 1
    assert funnel.failures["parse_error"] == 0


def test_record_outcome_success_is_silent():
    funnel = LLMFunnel()
    assert funnel.record_outcome(content="- ok", finish_reason="stop") == ""
    assert funnel.record_outcome(content="- ok") == ""
    assert sum(funnel.failures.values()) == 0


def test_record_failure_unknown_kind_maps_to_other():
    funnel = LLMFunnel()
    assert funnel.record_failure("galactic") == "other"
    assert funnel.failures["other"] == 1


def test_failure_event_emitted():
    funnel = LLMFunnel()
    funnel.record_failure("parse_error", "bad json")
    events = [e.event for e in funnel.lifecycle.replay()]
    assert "llm_failure" in events


def test_meter_view_shape():
    budget = _Budget()
    funnel = LLMFunnel(budget_ref=budget)
    funnel.post_llm(_Ctx(), "n", usage={"prompt_tokens": 10, "completion_tokens": 5})
    meter = funnel.meter()
    assert meter["calls"] == 1
    assert meter["prompt_tokens"] == 10
    assert meter["budget_spent_tokens"] == 15
    assert meter["budget_exceeded"] is False
    assert meter["failure_rate"] == 0.0
    assert set(meter["failures"]) == {"parse_error", "truncation", "other"}


def test_meter_empty_funnel():
    funnel = LLMFunnel()
    meter = funnel.meter()
    assert meter["calls"] == 0
    assert meter["failure_rate"] == 0.0
    assert meter["budget_spent_tokens"] is None
