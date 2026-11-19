from fusion_harness.observability import ObservabilityLoop, ObservabilityRecord
from fusion_harness.sidecar import PROTOCOL_VERSION, Harnessd


def test_record_and_snapshot():
    loop = ObservabilityLoop(agent_id="worker-1", run_id="r1")
    loop.record("tool_call", "plan", {"tool": "kb_search"}, args={"q": "x"})
    loop.record("error", "plan", {"kind": "tool_denied"})
    snap = loop.snapshot()
    assert snap["agent"] == "worker-1"
    assert snap["events"]["tool_call"] == 1
    assert snap["events"]["error"] == 1
    assert snap["records"] == 2


def test_attribution_produces_agent_step_mode():
    loop = ObservabilityLoop(agent_id="worker-1", run_id="r1")
    loop.record("tool_call", "planner", {"tool": "fetch"}, args={"url": "http://a"})
    loop.record("tool_call", "planner", {"tool": "fetch"}, args={"url": "http://a"})
    loop.record("error", "planner", {"kind": "tool_denied", "reason": "denied by policy"})
    rep = loop.attribution()
    assert rep["runs"]["r1"]["verdicts"], "no verdict for failing run"
    v = rep["runs"]["r1"]["verdicts"][0]
    assert v["agent"] == "planner"
    assert v["step"] == 2
    assert v["mode"]
    assert rep["mode_counts"]


def test_report_combines_snapshot_and_attribution():
    loop = ObservabilityLoop(agent_id="a", run_id="r")
    loop.record("error", "n", {"kind": "timeout"})
    rep = loop.report()
    assert "snapshot" in rep and "attribution" in rep
    assert rep["snapshot"]["events"]["error"] == 1


def test_ring_bound_drops_oldest():
    loop = ObservabilityLoop(run_id="r", max_records=10)
    for i in range(25):
        loop.record("tool_call", "n", {"i": i})
    assert loop.snapshot()["records"] == 10
    assert loop.records[0].payload["i"] == 15
    assert loop.snapshot()["events"]["records_dropped"] == 15


def test_observe_bus_wiring():
    from fusion_harness import LifecycleBus

    bus = LifecycleBus()
    loop = ObservabilityLoop(run_id="r")
    loop.observe_bus(bus)
    bus.emit("agent", "llm_before", {"node": "n1"}, session_id="r")
    assert loop.snapshot()["events"]["llm_before"] == 1
    assert loop.records[0].node_id == "n1"


def test_sidecar_report_end_to_end():
    h = Harnessd()
    assert (
        h.handle_sync(
            {"id": 1, "method": "hello", "params": {"protocol_version": PROTOCOL_VERSION}}
        )["result"]["protocol_version"]
        == PROTOCOL_VERSION
    )
    h.handle_sync(
        {
            "id": 2,
            "method": "tool_admit",
            "params": {
                "tool_name": "shell_exec",
                "args": {"command": "curl https://x"},
                "node_label": "planner",
                "graph_id": "g1",
            },
        }
    )
    rep = h.handle_sync({"id": 3, "method": "report"})["result"]
    assert rep["snapshot"]["events"]["tool_call"] == 1
    assert rep["snapshot"]["llm"] is not None
    assert "runs" in rep["attribution"]


def test_sidecar_report_methods_advertised():
    h = Harnessd()
    hello = h.handle_sync(
        {"id": 1, "method": "hello", "params": {"protocol_version": PROTOCOL_VERSION}}
    )
    assert "report" in hello["result"]["methods"]
    assert "observability" in hello["result"]["features"]


def test_record_shape_matches_analyzer_contract():
    rec = ObservabilityRecord(ts=1.0, run_id="r", node_id="n", event="error", payload={"a": 1})
    assert getattr(rec, "run_id") == "r"
    assert getattr(rec, "node_id") == "n"
    assert getattr(rec, "event") == "error"
