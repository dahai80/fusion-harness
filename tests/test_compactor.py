"""Compactor unit coverage (issue #2 R3)."""


from fusion_harness.compactor import CompactionConfig, Compactor, RetentionList


def _cfg(**over) -> CompactionConfig:
    base = {
        "context_window": 1000,
        "warning_buffer": 200,
        "error_buffer": 400,
        "manual_buffer": 100,
        "keep_recent_rounds": 1,
    }
    base.update(over)
    return CompactionConfig(**base)


def _msgs(n, size=100):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size}
            for i in range(n)]


def test_thresholds_derived_from_buffers():
    cfg = _cfg()
    assert cfg.warning_threshold() > cfg.error_threshold()
    assert cfg.manual_threshold() > cfg.warning_threshold()


def test_should_compact_none_below_warning():
    compactor = Compactor(_cfg())
    assert compactor.should_compact(_msgs(2, size=50)) == "none"


def test_should_compact_error_at_saturation():
    compactor = Compactor(_cfg())
    assert compactor.should_compact(_msgs(40, size=100)) == "error"


def test_estimate_tokens_monotonic():
    compactor = Compactor(_cfg())
    small = compactor.estimate_tokens(_msgs(1, size=10))
    big = compactor.estimate_tokens(_msgs(10, size=100))
    assert 0 < small < big


def test_smart_truncate_keeps_recent_rounds():
    compactor = Compactor(_cfg(keep_recent_rounds=2))
    msgs = _msgs(16, size=300)
    out = compactor.compact(msgs, "warning")
    assert len(out) < len(msgs)
    assert out[-1]["content"] == msgs[-1]["content"]


def test_hard_compact_drops_more_than_smart():
    compactor = Compactor(_cfg())
    msgs = _msgs(30, size=200)
    smart = compactor.compact(msgs, "warning")
    hard = compactor.compact(msgs, "error")
    assert len(hard) <= len(smart)


def test_compact_preserves_system_messages():
    compactor = Compactor(_cfg())
    msgs = [{"role": "system", "content": "you are an agent"}] + _msgs(20, size=200)
    out = compactor.compact(msgs, "error")
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "you are an agent"


def test_compact_injects_retention_header():
    compactor = Compactor(_cfg())
    msgs = (
        [{"role": "system", "content": "you are an agent"}]
        + [{"role": "user", "content": "TODO: finish the login flow"}]
        + _msgs(20, size=200)
    )
    out = compactor.compact(msgs, "error")
    assert any("[Retention List]" in m.get("content", "") for m in out[:4])
    assert any("finish the login flow" in m.get("content", "") for m in out[:4])


def test_compact_level_none_is_noop():
    compactor = Compactor(_cfg())
    msgs = _msgs(4, size=50)
    out = compactor.compact(msgs, "none")
    assert out == msgs


def test_reactive_strip_removes_oversized_tool_output():
    compactor = Compactor(_cfg())
    msgs = [
        {"role": "user", "content": "run"},
        {"role": "tool", "content": "y" * 100000},
    ]
    out = compactor.reactive_strip(msgs)
    assert len(out[1]["content"]) < len(msgs[1]["content"])


def test_deterministic_summary_present_after_compact():
    compactor = Compactor(_cfg())
    msgs = _msgs(20, size=200)
    out = compactor.compact(msgs, "warning")
    assert any("[Compacted prior context]" in m.get("content", "") for m in out)


def test_mlx_toggle_env(monkeypatch):
    monkeypatch.setenv("FUSION_COMPACT_MLX", "0")
    compactor = Compactor(_cfg())
    assert not compactor._mlx_enabled()
    monkeypatch.setenv("FUSION_COMPACT_MLX", "1")
    assert compactor._mlx_enabled()
    monkeypatch.delenv("FUSION_COMPACT_MLX")
    assert compactor._mlx_enabled()


def test_retention_list_extract_and_render():
    msgs = [
        {"role": "user", "content": "TODO: finish login flow"},
        {"role": "assistant", "content": "constraint: port must stay 11434"},
    ]
    rl = RetentionList.from_messages(msgs)
    assert rl.non_empty()
    rendered = rl.render()
    assert "finish login flow" in rendered
    assert "[Retention List]" in rendered


def test_retention_list_empty():
    rl = RetentionList.from_messages(_msgs(3))
    assert not rl.non_empty()
