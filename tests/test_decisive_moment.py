"""Issue #123: decisive-moment localization + evidence-distance on FailureVerdict."""

from fusion_harness import FailureAnalyzer
from fusion_harness.failure_attribution import FailureVerdict


class _Rec:
    def __init__(self, run_id, ts, event, node_id="agent-a", payload=None):
        self.run_id = run_id
        self.ts = ts
        self.event = event
        self.node_id = node_id
        self.payload = payload or {}


def _run_records():
    # repeated identical tool call early, error event near the outcome
    return [
        _Rec("r1", 1, "tool_call", payload={"tool": "grep", "args": "x"}),
        _Rec("r1", 2, "tool_call", payload={"tool": "grep", "args": "x"}),
        _Rec("r1", 3, "tool_call", payload={"tool": "read", "args": "y"}),
        _Rec("r1", 4, "tool_call", payload={"tool": "read", "args": "z"}),
        _Rec("r1", 5, "tool_call", payload={"tool": "read", "args": "w"}),
        _Rec("r1", 6, "error", payload={"error": "invalid result", "status": "failed"}),
    ]


class TestEvidenceDistance:
    def test_verdict_carries_evidence_distance(self):
        report = FailureAnalyzer().analyze(_run_records())
        verdict = report["runs"]["r1"]["verdicts"][0]
        assert verdict["evidence_distance"] == 0  # error event is the last step

    def test_distance_counts_steps_from_outcome(self):
        # failure signal early, normal calls after, no error event:
        # the signal is the best candidate and sits 2 steps from the outcome
        recs = [
            _Rec("r1", 1, "tool_call", payload={"status": "fail", "error": "mismatch"}),
            _Rec("r1", 2, "tool_call", payload={"tool": "read", "args": "y"}),
            _Rec("r1", 3, "tool_call", payload={"tool": "read", "args": "z"}),
        ]
        report = FailureAnalyzer().analyze(recs)
        assert report["runs"]["r1"]["verdicts"][0]["evidence_distance"] == 2

    def test_pinpointer_path_sets_distance(self):
        recs = _run_records()
        analyzer = FailureAnalyzer(
            pinpointer=lambda recs, cands: (0, "unclear_goals", 0.9, "early")
        )
        report = analyzer.analyze(recs)
        verdict = report["runs"]["r1"]["verdicts"][0]
        assert verdict["step"] == 0
        assert verdict["evidence_distance"] == len(recs) - 1

    def test_decisive_moment_prefers_near_outcome(self):
        report = FailureAnalyzer().analyze(_run_records())
        dm = report["runs"]["r1"]["decisive_moment"]
        assert dm["step"] == 5
        assert dm["distance"] == 0
        assert dm["score"] > 0

    def test_distance_penalizes_score(self):
        # same failure signal, once early and once as the outcome:
        # the near-outcome occurrence scores strictly higher
        early = [
            _Rec("r2", 1, "tool_call", payload={"status": "fail", "error": "mismatch"}),
            _Rec("r2", 2, "tool_call", payload={"tool": "read", "args": "y"}),
            _Rec("r2", 3, "tool_call", payload={"tool": "read", "args": "z"}),
        ]
        late = list(early[1:]) + [
            _Rec("r3", 4, "tool_call", payload={"status": "fail", "error": "mismatch"})
        ]
        dm_early = FailureAnalyzer().analyze(early)["runs"]["r2"]["decisive_moment"]
        dm_late = FailureAnalyzer().analyze(late)["runs"]["r3"]["decisive_moment"]
        assert dm_early["distance"] == 2
        assert dm_late["distance"] == 0
        assert dm_late["score"] > dm_early["score"]

    def test_to_dict_roundtrip(self):
        v = FailureVerdict("r", "a", 1, "unclear_goals", "specification_system_design", 0.5, "e", 4)
        assert v.to_dict()["evidence_distance"] == 4
