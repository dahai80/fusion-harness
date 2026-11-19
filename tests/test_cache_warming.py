"""Tests for issue #96: cache-warming payback gate — warm only when the
expected saving beats the warming cost; typed decision event per prefix;
pure offline on synthetic telemetry series."""

import pytest

from fusion_harness.cache_warming import (
    COLD_PREFILL_RATE,
    WARM_MARGIN,
    WARM_RATE,
    CacheWarmingGate,
    PrefixTelemetry,
)


def _gate(**kw):
    return CacheWarmingGate(**kw)


def test_high_reuse_large_prefix_warms():
    gate = _gate()
    decision = gate.decide(PrefixTelemetry("sys-prompt-a", 4000, reuse_rate=0.95))
    assert decision.warm is True
    assert decision.reason == "payback exceeds margin"
    # saving = 0.95 * 4000 * 1.0 - 4000 * 0.3 = 2600 > margin * 1200
    assert decision.expected_saving == pytest.approx(2600.0)
    assert decision.warm_cost == pytest.approx(1200.0)


def test_low_reuse_skips():
    gate = _gate()
    decision = gate.decide(PrefixTelemetry("one-shot", 4000, reuse_rate=0.05))
    assert decision.warm is False
    assert decision.reason == "reuse rate below floor"


def test_tiny_prefix_skips():
    gate = _gate()
    decision = gate.decide(PrefixTelemetry("tiny", 10, reuse_rate=0.99))
    assert decision.warm is False
    assert decision.reason == "prefix below minimum size"


def test_marginal_payback_skips():
    # saving = 0.5*4000*1.0 - 1200 = 800; margin requires > 2*1200 = 2400
    gate = _gate()
    decision = gate.decide(PrefixTelemetry("meh", 4000, reuse_rate=0.5))
    assert decision.warm is False
    assert decision.reason == "payback below margin"


def test_reuse_rate_clamped():
    gate = _gate()
    decision = gate.decide(PrefixTelemetry("x", 4000, reuse_rate=5.0))
    assert decision.expected_saving == pytest.approx(4000.0 - 1200.0)


def test_invalid_telemetry_rejected():
    with pytest.raises(ValueError):
        PrefixTelemetry("", 100, reuse_rate=0.5)
    with pytest.raises(ValueError):
        PrefixTelemetry("p", -5, reuse_rate=0.5)


def test_observe_and_decide_batch_and_snapshot():
    gate = _gate()
    batch = [
        PrefixTelemetry("hot", 4000, reuse_rate=0.95),
        PrefixTelemetry("cold", 4000, reuse_rate=0.01),
        PrefixTelemetry("small", 50, reuse_rate=0.9),
    ]
    decisions = gate.observe_and_decide(batch)
    assert [d.warm for d in decisions] == [True, False, False]
    snap = gate.snapshot()
    assert snap["decisions"] == 3
    assert snap["warm"] == 1
    assert snap["warm_list"] == ["hot"]


def test_decision_event_is_jsonable():
    decision = _gate().decide(PrefixTelemetry("p", 4000, reuse_rate=0.9))
    blob = decision.to_dict()
    assert set(blob) == {"prefix_id", "warm", "reason", "expected_saving", "warm_cost"}


def test_host_boundary_no_provider_calls():
    # The gate is pure decision logic: nothing in the module dials out.
    import fusion_harness.cache_warming as cw

    source = open(cw.__file__, encoding="utf-8").read()
    for banned in ("http", "urllib", "socket", "requests", "MLXClient"):
        assert banned not in source, banned


def test_cost_constants_sane():
    assert WARM_RATE < COLD_PREFILL_RATE
    assert WARM_MARGIN >= 1.0
