"""Issue #124: fan-out budget ladder + token-multiplier warning in SpawnBrief."""

import pytest

from fusion_harness import (
    FANOUT_COMPLEX_MAX,
    FANOUT_HARD_MAX,
    FANOUT_SIMPLE_MAX,
    MULTI_AGENT_TOKEN_MULTIPLIER,
    TOKEN_MULTIPLIER_WARNING,
    SpawnBrief,
    SpawnBriefError,
    fanout_warnings,
)


def _brief(concurrency=3, token_budget=5000):
    return SpawnBrief(
        objective="scan module X",
        output_format="json findings list",
        tool_guidance="use ripgrep",
        boundaries="read-only",
        token_budget=token_budget,
        concurrency=concurrency,
        depth=1,
    )


class TestFanoutLadder:
    def test_ladder_constants(self):
        assert FANOUT_SIMPLE_MAX == 5
        assert FANOUT_COMPLEX_MAX == 15
        assert FANOUT_HARD_MAX == 20
        assert MULTI_AGENT_TOKEN_MULTIPLIER == 15
        assert "15x" in TOKEN_MULTIPLIER_WARNING

    def test_simple_band_no_warning(self):
        assert fanout_warnings(_brief(concurrency=5)) == []

    def test_above_simple_band_warns(self):
        warnings = fanout_warnings(_brief(concurrency=8))
        assert len(warnings) == 1
        assert "between the simple" in warnings[0]
        assert "15x" in warnings[0]

    def test_complex_band_warns(self):
        warnings = fanout_warnings(_brief(concurrency=12))
        assert warnings == []  # 10-15 is the sanctioned complex band

    def test_above_complex_band_warns(self):
        warnings = fanout_warnings(_brief(concurrency=18))
        assert len(warnings) == 1
        assert "complex-objective band" in warnings[0]

    def test_hard_max_errors(self):
        with pytest.raises(SpawnBriefError, match="fan-out hard max 20"):
            _brief(concurrency=21)

    def test_at_hard_max_valid(self):
        brief = _brief(concurrency=20)
        assert brief.concurrency == 20

    def test_post_init_logs_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="fusion_harness.spawn_protocol"):
            _brief(concurrency=18)
        assert any("fanout guidance" in r.message for r in caplog.records)
