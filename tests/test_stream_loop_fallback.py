"""Tests for issue #95: stream loop detection + model fallback signal on
output-token-limit truncation. Loops are budget leaks — detected across
thinking/text/tool_args surfaces; a call that never finished emits a typed
model_fallback_required event (the host decides the fallback model)."""

import pytest

from fusion_harness.funnels import LLMFunnel, ToolFunnel
from fusion_harness.stream_guard import SURFACES, LoopDetector


class _Ctx:
    session_id = "s1"
    graph_id = "g1"


class TestLoopDetector:
    def test_unknown_surface_rejected(self):
        detector = LoopDetector()
        with pytest.raises(ValueError):
            detector.observe("vibes", "some content here")

    def test_no_loop_below_threshold(self):
        detector = LoopDetector(max_repeats=3)
        for _ in range(2):
            assert detector.observe("text", "same repeated content") is None

    def test_loop_detected_on_consecutive_repeats(self):
        detector = LoopDetector(max_repeats=3)
        for _ in range(2):
            detector.observe("text", "same repeated content")
        event = detector.observe("text", "same repeated content")
        assert event is not None
        assert event["surface"] == "text"
        assert event["repeats"] == 3

    def test_interrupted_streak_not_a_loop(self):
        detector = LoopDetector(max_repeats=3)
        detector.observe("text", "same repeated content")
        detector.observe("text", "different content entirely")
        assert detector.observe("text", "same repeated content") is None

    def test_tool_args_surface_dict_content(self):
        detector = LoopDetector(max_repeats=2)
        args = {"tool": "shell", "args": {"command": "ls"}}
        assert detector.observe("tool_args", args) is None
        event = detector.observe("tool_args", dict(args))
        assert event is not None and event["surface"] == "tool_args"

    def test_short_content_ignored(self):
        detector = LoopDetector(max_repeats=2, min_content_len=50)
        for _ in range(3):
            assert detector.observe("text", "tiny") is None

    def test_reset_clears_state(self):
        detector = LoopDetector(max_repeats=2)
        detector.observe("thinking", "long thinking block content")
        detector.reset("thinking")
        assert detector.observe("thinking", "long thinking block content") is None

    def test_sensitivity_configurable(self):
        strict = LoopDetector(max_repeats=5)
        for _ in range(4):
            assert strict.observe("text", "repeated streaming output") is None
        assert strict.observe("text", "repeated streaming output") is not None

    def test_surfaces_contract(self):
        assert set(SURFACES) == {"thinking", "text", "tool_args"}


class TestFunnelWiring:
    @pytest.mark.asyncio
    async def test_text_loop_emits_event_and_counts(self):
        funnel = LLMFunnel()
        await funnel.pre_llm(_Ctx(), "n")
        for _ in range(3):
            funnel.post_llm(_Ctx(), "n", usage={}, content="looping same output body")
        assert funnel.loops_detected == 1
        assert funnel.meter()["loops_detected"] == 1
        events = [e.event for e in funnel.lifecycle.replay()]
        assert "llm_loop_detected" in events

    @pytest.mark.asyncio
    async def test_truncation_emits_fallback_signal(self):
        funnel = LLMFunnel()
        await funnel.pre_llm(_Ctx(), "n")
        funnel.post_llm(_Ctx(), "n", usage={}, content="partial", finish_reason="length")
        assert funnel.fallback_signals == 1
        assert funnel.meter()["fallback_signals"] == 1
        events = [e.event for e in funnel.lifecycle.replay()]
        assert "model_fallback_required" in events
        # Parse errors are NOT fallback signals — repair handles those.
        funnel.post_llm(_Ctx(), "n", usage={}, content="{}", parse_error=True)
        assert funnel.fallback_signals == 1
        assert funnel.failures["parse_error"] == 1

    @pytest.mark.asyncio
    async def test_clean_call_no_signals(self):
        funnel = LLMFunnel()
        await funnel.pre_llm(_Ctx(), "n")
        funnel.post_llm(_Ctx(), "n", usage={}, content="all good", finish_reason="stop")
        assert funnel.fallback_signals == 0
        assert funnel.loops_detected == 0

    @pytest.mark.asyncio
    async def test_toolfunnel_loop_detector_wired(self, monkeypatch):
        monkeypatch.setenv("FUSION_FUNNEL_PERMISSIONS", "off")
        funnel = ToolFunnel()
        funnel.loop_detector = LoopDetector(max_repeats=2)
        args = {"command": "ls -la"}
        await funnel.admit(_Ctx(), "shell_exec", args)
        await funnel.admit(_Ctx(), "shell_exec", dict(args))
        events = [e.event for e in funnel.lifecycle.replay()]
        assert "tool_loop_detected" in events
