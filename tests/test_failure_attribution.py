"""Tests for issue #93: failure attribution analyzer — (agent, step,
MAST-mode) verdicts over recorded trajectories, offline, pinpointer
mockable."""

import json

from fusion_harness.failure_attribution import (
    MAST_CLUSTERS,
    MASTMode,
    FailureAnalyzer,
    cluster_of,
)
from fusion_harness.trajectory import TrajectoryRecord


def _rec(run_id, step, event="tool_call", node="alpha", payload=None, ts=0):
    return TrajectoryRecord(
        run_id=run_id,
        node_id=node,
        ts=ts,
        event=event,
        payload=payload or {},
    )


def test_mast_vocabulary_pinned():
    assert len(MASTMode) == 14
    assert sum(len(modes) for modes in MAST_CLUSTERS.values()) == 14
    for mode in MASTMode:
        assert cluster_of(mode) != "unclustered"


def test_no_candidates_no_verdicts():
    analyzer = FailureAnalyzer()
    recs = [_rec("r1", i, event="tool_result", payload={"status": "ok"}) for i in range(5)]
    report = analyzer.analyze(recs)
    assert report["runs"]["r1"]["verdicts"] == []
    assert report["mode_counts"] == {}


def test_error_event_attribution():
    recs = [
        _rec("r1", 0, event="start", payload={}, ts=1),
        _rec("r1", 1, event="tool_call", payload={"tool": "read", "args": {}}, ts=2),
        _rec(
            "r1",
            2,
            event="error",
            node="beta",
            payload={"error": "unverified output accepted, skipped check"},
            ts=3,
        ),
        _rec("r1", 3, event="end", payload={}, ts=4),
    ]
    report = FailureAnalyzer().analyze(recs)
    run = report["runs"]["r1"]
    v = run["verdicts"][0]
    assert v["agent"] == "beta"
    assert v["step"] == 2
    assert v["mode"] == MASTMode.INCOMPLETE_VERIFICATION.value
    assert v["cluster"] == "task_verification"
    assert run["responsible_agent"] == "beta"


def test_repeated_identical_tool_call_is_candidate():
    analyzer = FailureAnalyzer()
    recs = [
        _rec("r2", 0, event="tool_call", payload={"tool": "search", "args": {"q": "x"}}, ts=1),
        _rec("r2", 1, event="tool_call", payload={"tool": "search", "args": {"q": "x"}}, ts=2),
        _rec("r2", 2, event="tool_call", payload={"tool": "search", "args": {"q": "x"}}, ts=3),
    ]
    candidates = analyzer._coarse(recs)
    assert any("repeated identical" in c.reason for c in candidates)


def test_keyword_classified_mode():
    recs = [
        _rec(
            "r3",
            0,
            event="error",
            node="gamma",
            payload={"error": "task aborted: budget exhausted, gave up"},
            ts=1,
        ),
    ]
    report = FailureAnalyzer().analyze(recs)
    v = report["runs"]["r3"]["verdicts"][0]
    assert v["mode"] == MASTMode.PREMATURE_TERMINATION.value


def test_custom_pinpointer_overrides():
    def pinpointer(recs, candidates):
        return (1, MASTMode.UNCLEAR_ROLES.value, 0.9, "mock evidence")

    recs = [
        _rec("r4", 0, event="tool_call", node="delta", payload={}, ts=1),
        _rec("r4", 1, event="error", node="delta", payload={"error": "boom"}, ts=2),
    ]
    report = FailureAnalyzer(pinpointer=pinpointer).analyze(recs)
    v = report["runs"]["r4"]["verdicts"][0]
    assert v["mode"] == "unclear_roles"
    assert v["confidence"] == 0.9
    assert v["evidence"] == "mock evidence"


def test_pinpointer_unknown_mode_falls_back():
    def pinpointer(recs, candidates):
        return (0, "totally_unknown_mode", 0.5, "")

    recs = [_rec("r5", 0, event="error", payload={"error": "boom"}, ts=1)]
    report = FailureAnalyzer(pinpointer=pinpointer).analyze(recs)
    v = report["runs"]["r5"]["verdicts"][0]
    assert v["mode"] in {m.value for m in MASTMode}
    assert v["cluster"] != "unclustered"


def test_report_is_json_serializable():
    recs = [_rec("r6", 0, event="error", payload={"error": "timeout hit"}, ts=1)]
    report = FailureAnalyzer().analyze(recs)
    blob = json.dumps(report)
    assert "verdicts" in blob


def test_multiple_runs_aggregate():
    recs = []
    for run in ("a", "b"):
        recs.append(_rec(run, 0, event="error", node="n1", payload={"error": "fail"}, ts=1))
    report = FailureAnalyzer().analyze(recs)
    assert set(report["runs"]) == {"a", "b"}
    assert report["mode_counts"]
