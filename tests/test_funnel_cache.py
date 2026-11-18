"""Tests for the KV-cache axis in funnel accounting (issue #76): cached
input extraction from provider usage payloads, per-run + aggregate hit
rate, append-only prefix stability, and compaction-break visibility."""

import asyncio
from types import SimpleNamespace

from fusion_harness.funnels import CacheTelemetry, LLMFunnel


class _Compactor:
    def __init__(self, rewrite=False):
        self.rewrite = rewrite

    def should_compact(self, messages):
        return "manual" if self.rewrite and len(messages) > 1 else "none"

    def compact(self, messages, level):
        if not self.rewrite:
            return messages
        return [messages[0]] + [{"role": "system", "content": "summary"}] + messages[1:]


def _ctx():
    return SimpleNamespace(session_id="s1", graph_id="")


class TestUsageExtraction:
    def test_openai_style_cached_in_prompt_tokens(self):
        total, cached = CacheTelemetry._extract(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}
        )
        assert (total, cached) == (100, 80)

    def test_anthropic_style_cache_read_excluded_from_input(self):
        total, cached = CacheTelemetry._extract({"input_tokens": 20, "cache_read_input_tokens": 80})
        assert (total, cached) == (100, 80)

    def test_generic_cached_tokens_key(self):
        total, cached = CacheTelemetry._extract({"prompt_tokens": 50, "cached_tokens": 10})
        assert (total, cached) == (50, 10)

    def test_no_cache_field_hit_zero(self):
        total, cached = CacheTelemetry._extract({"prompt_tokens": 50})
        assert (total, cached) == (50, 0)

    def test_empty_and_none(self):
        assert CacheTelemetry._extract({}) == (0, 0)
        assert CacheTelemetry._extract(None) == (0, 0)


class TestHitRate:
    def test_hit_rate_mixed_calls(self):
        axis = CacheTelemetry()
        axis.record_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}})
        axis.record_usage({"prompt_tokens": 50})
        snap = axis.snapshot()
        assert snap["input_tokens"] == 150
        assert snap["cached_input_tokens"] == 80
        assert snap["hit_rate"] == round(80 / 150, 6)

    def test_run_snapshot_resets_aggregate_keeps(self):
        axis = CacheTelemetry()
        axis.record_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}})
        run1 = axis.run_snapshot()
        axis.start_run()
        axis.record_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 90}})
        run2 = axis.run_snapshot()
        assert run1["hit_rate"] == 0.4
        assert run2["hit_rate"] == 0.9
        assert axis.snapshot()["hit_rate"] == round(130 / 200, 6)

    def test_zero_input_hit_rate_zero(self):
        assert CacheTelemetry().snapshot()["hit_rate"] == 0.0


class TestPrefixStability:
    def _msgs(self, n):
        return [{"role": "user", "content": "m%d" % i} for i in range(n)]

    def test_append_only_is_stable(self):
        axis = CacheTelemetry()
        assert axis.observe_prefix(self._msgs(2)) is None
        assert axis.observe_prefix(self._msgs(3)) is None
        snap = axis.snapshot()
        assert snap["prefix_breaks"] == 0
        assert snap["prefix_stability"] == 1.0

    def test_middle_rewrite_is_break_with_diverge_index(self):
        axis = CacheTelemetry()
        axis.observe_prefix(self._msgs(3))
        msgs = self._msgs(4)
        msgs[1]["content"] = "rewritten"
        event = axis.observe_prefix(msgs)
        assert event["diverge_index"] == 1
        snap = axis.snapshot()
        assert snap["prefix_breaks"] == 1
        assert snap["prefix_stability"] == 0.5

    def test_shrink_is_break(self):
        axis = CacheTelemetry()
        axis.observe_prefix(self._msgs(4))
        event = axis.observe_prefix(self._msgs(2))
        assert event["diverge_index"] == 2

    def test_break_events_bounded(self):
        axis = CacheTelemetry(max_break_events=3)
        for i in range(6):
            axis.observe_prefix([{"role": "user", "content": "v%d" % i}])
        assert len(axis.break_events) == 3
        assert axis.break_events[-1]["step"] == 6


class TestFunnelWiring:
    def test_post_llm_records_cache_and_payload(self):
        funnel = LLMFunnel()
        funnel.post_llm(
            _ctx(),
            usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 70}},
        )
        assert funnel.cache.snapshot()["hit_rate"] == 0.7
        bus_events = funnel.lifecycle.replay()
        after = [e for e in bus_events if e.event == "llm_after"]
        assert after[0].payload["cache"] == {"input_tokens": 100, "cached_input_tokens": 70}

    def test_pre_llm_stable_prefix_no_break(self):
        funnel = LLMFunnel()
        base = [{"role": "system", "content": "sys"}]

        async def scenario():
            ok1, _, _ = await funnel.pre_llm(
                _ctx(), messages=base + [{"role": "user", "content": "a"}]
            )
            ok2, _, _ = await funnel.pre_llm(
                _ctx(),
                messages=base
                + [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
            )
            return ok1, ok2

        ok1, ok2 = asyncio.run(scenario())
        assert ok1 and ok2
        assert funnel.cache.snapshot()["prefix_breaks"] == 0

    def test_compaction_unstable_prefix_visible(self):
        funnel = LLMFunnel(compactor=_Compactor(rewrite=True))
        base = [{"role": "system", "content": "sys"}]

        async def scenario():
            await funnel.pre_llm(_ctx(), messages=base + [{"role": "user", "content": "a"}])
            await funnel.pre_llm(_ctx(), messages=base + [{"role": "user", "content": "b"}])

        asyncio.run(scenario())
        snap = funnel.cache.snapshot()
        assert snap["prefix_breaks"] >= 1
        breaks = [e for e in funnel.lifecycle.replay() if e.event == "prefix_break"]
        assert breaks

    def test_run_boundary_via_funnel(self):
        funnel = LLMFunnel()
        funnel.post_llm(_ctx(), usage={"prompt_tokens": 10})
        funnel.cache.start_run()
        funnel.post_llm(_ctx(), usage={"prompt_tokens": 30, "cached_tokens": 30})
        run = funnel.cache.run_snapshot()
        assert run["hit_rate"] == 1.0
        assert funnel.cache.snapshot()["cached_input_tokens"] == 30
