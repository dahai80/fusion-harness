"""Issue #122: threshold-triggered compaction policy on CompactionConfig."""

import pytest

from fusion_harness import CompactionConfig, Compactor


class TestTriggerPolicy:
    def test_default_off_no_behavior_change(self):
        cfg = CompactionConfig()
        assert cfg.trigger_ratio is None
        assert cfg.trigger_threshold() is None
        assert cfg.should_auto_compact(cfg.context_window) is False
        assert cfg.should_auto_compact(10**9) is False

    def test_threshold_derived_from_ratio(self):
        cfg = CompactionConfig(context_window=32768, trigger_ratio=0.85)
        assert cfg.trigger_threshold() == int(32768 * 0.85)
        assert cfg.should_auto_compact(int(32768 * 0.85)) is True

    def test_below_threshold_not_due(self):
        cfg = CompactionConfig(context_window=1000, trigger_ratio=0.9)
        assert cfg.should_auto_compact(899) is False
        assert cfg.should_auto_compact(900) is True

    def test_invalid_ratio_raises(self):
        for bad in (0.0, -0.5, 1.01, 2.0):
            with pytest.raises(ValueError):
                CompactionConfig(trigger_ratio=bad)

    def test_ratio_one_valid(self):
        cfg = CompactionConfig(context_window=100, trigger_ratio=1.0)
        assert cfg.trigger_threshold() == 100
        assert cfg.should_auto_compact(100) is True

    def test_buffer_thresholds_unchanged(self):
        cfg = CompactionConfig(context_window=32768, trigger_ratio=0.8)
        assert cfg.warning_threshold() == cfg.context_window - cfg.warning_buffer
        assert cfg.error_threshold() == cfg.context_window - cfg.error_buffer
        assert cfg.manual_threshold() == cfg.context_window - cfg.manual_buffer

    def test_compactor_uses_policy(self):
        compactor = Compactor(CompactionConfig(context_window=100, trigger_ratio=0.5))
        assert compactor.config.should_auto_compact(60) is True
        assert compactor.config.should_auto_compact(40) is False
