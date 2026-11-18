"""Tests for telemetry-only annotations (issue #55): discardable trajectory
spans + per-agent prefix stability reporting."""

import os

import pytest

from fusion_harness.trajectory import (
    DISCARD_REASONS,
    PrefixStabilityTracker,
    TrajectoryReader,
    TrajectoryRecord,
    TrajectorySchemaError,
    TrajectoryWriter,
    mark_discardable,
)


def _rec(event="tool_result", **kw):
    return TrajectoryRecord(run_id="r1", node_id="n1", event=event, **kw)


class TestDiscardableSpans:
    def test_mark_returns_annotated_copy(self):
        rec = _rec(payload={"bytes": 500})
        out = mark_discardable(rec, "stale_tool_output")
        assert out.payload["discardable"] == "stale_tool_output"
        assert out.payload["bytes"] == 500
        assert "discardable" not in rec.payload

    def test_both_reasons_allowed(self):
        for reason in DISCARD_REASONS:
            assert mark_discardable(_rec(), reason).payload["discardable"] == reason

    def test_unknown_reason_rejected(self):
        with pytest.raises(TrajectorySchemaError, match="unknown discard reason"):
            mark_discardable(_rec(), "because")

    def test_annotation_survives_jsonl_round_trip(self, tmp_path):
        path = str(tmp_path / "traj.jsonl")
        writer = TrajectoryWriter(path)
        writer.write(mark_discardable(_rec(payload={"bytes": 10}), "superseded_turn"))
        writer.write(_rec())
        records = TrajectoryReader(path).read_all()
        assert records[0].payload["discardable"] == "superseded_turn"
        assert "discardable" not in records[1].payload

    def test_privacy_rule_still_enforced_on_meta(self):
        with pytest.raises(TrajectorySchemaError, match="must not carry"):
            mark_discardable(
                _rec(event="model_call_meta", payload={"prompt": "x"}), "superseded_turn"
            )


class TestPrefixStability:
    def test_stable_prefix_ratio_one(self):
        t = PrefixStabilityTracker()
        for _ in range(5):
            t.observe("a1", "sys prompt", "first user turn")
        assert t.stability("a1") == 1.0

    def test_changing_prefix_lowers_ratio(self):
        t = PrefixStabilityTracker()
        for i in range(4):
            t.observe("a1", "sys prompt v%d" % i, "first user turn")
        assert t.stability("a1") == 0.0

    def test_single_turn_baseline_only(self):
        t = PrefixStabilityTracker()
        t.observe("a1", "sys", "u")
        assert t.stability("a1") == 0.0
        assert t.report()["a1"]["turns"] == 1

    def test_per_agent_separation(self):
        t = PrefixStabilityTracker()
        for _ in range(3):
            t.observe("stable-agent", "sys", "u1")
        for i in range(3):
            t.observe("churn-agent", "sys v%d" % i, "u1")
        report = t.report()
        assert report["stable-agent"]["stability"] == 1.0
        assert report["churn-agent"]["stability"] == 0.0
        assert report["stable-agent"]["turns"] == 3

    def test_observe_messages_extracts_head(self):
        t = PrefixStabilityTracker()
        msgs = [
            {"role": "system", "content": "sys head"},
            {"role": "user", "content": "first turn"},
            {"role": "user", "content": "second turn"},
        ]
        t.observe_messages("a1", msgs)
        t.observe_messages("a1", msgs + [{"role": "user", "content": "more"}])
        assert t.stability("a1") == 1.0

    def test_no_turns_zero(self):
        assert PrefixStabilityTracker().report() == {}
        assert PrefixStabilityTracker().stability("ghost") == 0.0

    def test_report_rounding(self):
        t = PrefixStabilityTracker()
        for i in range(3):
            t.observe("a1", "sys" if i < 2 else "other", "u")
        assert t.report()["a1"]["stability"] == 0.5


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "trajectory.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
