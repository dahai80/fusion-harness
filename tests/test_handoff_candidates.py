"""Issue #132: edge-level (handoff) failure candidates in FailureAnalyzer."""

from fusion_harness import FailureAnalyzer
from fusion_harness.failure_attribution import HANDOFF_WINDOW


class _Rec:
    def __init__(self, run_id, ts, event, node_id, payload=None):
        self.run_id = run_id
        self.ts = ts
        self.event = event
        self.node_id = node_id
        self.payload = payload or {}


def _analysis(recs):
    return FailureAnalyzer().analyze(recs)["runs"]["r1"]


class TestHandoffCandidates:
    def test_additive_key_always_present(self):
        recs = [_Rec("r1", 1, "tool_call", "a", payload={"tool": "t", "args": "x"})]
        report = _analysis(recs)
        assert report["handoff_candidates"] == []

    def test_failure_after_handoff_detected(self):
        recs = [
            _Rec("r1", 1, "tool_call", "a", payload={"tool": "t", "args": "x"}),
            _Rec("r1", 2, "tool_call", "b", payload={"tool": "u", "args": "y"}),
            _Rec("r1", 3, "error", "b", payload={"error": "boom", "status": "fail"}),
        ]
        cands = _analysis(recs)["handoff_candidates"]
        assert len(cands) == 1
        assert cands[0]["from_node"] == "a"
        assert cands[0]["to_node"] == "b"
        assert cands[0]["step"] == 1
        assert cands[0]["distance"] == 1

    def test_no_handoff_same_node_failure(self):
        recs = [
            _Rec("r1", 1, "tool_call", "a", payload={"tool": "t", "args": "x"}),
            _Rec("r1", 2, "error", "a", payload={"error": "boom"}),
        ]
        assert _analysis(recs)["handoff_candidates"] == []

    def test_failure_beyond_window_ignored(self):
        gap = HANDOFF_WINDOW + 1
        recs = [_Rec("r1", i, "tool_call", "a", payload={"tool": "t", "args": "x"}) for i in range(1, gap)]
        recs += [
            _Rec("r1", gap, "tool_call", "b", payload={"tool": "u", "args": "y"}),
            _Rec("r1", gap + 1, "error", "b", payload={"error": "boom"}),
        ]
        # error at distance 1 from the switch (b starts at step gap)
        cands = _analysis(recs)["handoff_candidates"]
        assert len(cands) == 1
        # now push the error beyond the window with neutral b-steps
        recs2 = [_Rec("r1", i, "tool_call", "a", payload={"tool": "t", "args": "x"}) for i in range(1, gap)]
        recs2 += [_Rec("r1", gap, "tool_call", "b", payload={"tool": "u", "args": "y"})]
        recs2 += [
            _Rec("r1", gap + k, "tool_call", "b", payload={"tool": "v", "args": "z%d" % k})
            for k in range(1, HANDOFF_WINDOW + 1)
        ]
        recs2 += [_Rec("r1", 99, "error", "b", payload={"error": "boom"})]
        assert _analysis(recs2)["handoff_candidates"] == []

    def test_multiple_handoffs(self):
        recs = [
            _Rec("r1", 1, "tool_call", "a", payload={"tool": "t", "args": "x"}),
            _Rec("r1", 2, "error", "b", payload={"error": "x"}),  # a->b handoff, failure at switch
            _Rec("r1", 3, "tool_call", "b", payload={"tool": "u", "args": "y"}),
            _Rec("r1", 4, "error", "b", payload={"error": "y"}),  # same node, no handoff
        ]
        cands = _analysis(recs)["handoff_candidates"]
        assert {(c["from_node"], c["to_node"]) for c in cands} == {("a", "b")}
        assert cands[0]["distance"] == 0
