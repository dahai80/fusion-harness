"""K3 tests (issue #36): ArtifactTools capture/read/list/diff +
trajectory writer/reader round-trip, schema rejection, legacy adapters."""

import tempfile

import pytest

from fusion_harness.artifact_channel import ArtifactStore, ArtifactTools
from fusion_harness.trajectory import (
    EVENTS,
    TrajectoryReader,
    TrajectoryRecord,
    TrajectorySchemaError,
    TrajectoryWriter,
    normalize_record,
)


@pytest.fixture
def tools():
    store = ArtifactStore(tempfile.mkdtemp(prefix="k3-artifacts-"))
    return ArtifactTools(store)


class TestArtifactTools:
    def test_capture_read_roundtrip(self, tools):
        ref = tools.capture("report", "body text", kind="md", tags=["scan"])
        assert ref == "shared/report.md"
        assert tools.read(ref) == "body text"

    def test_ref_immutability_identity(self, tools):
        ref = tools.capture("out", "content-v1")
        content = tools.read(ref)
        assert content == "content-v1"
        assert tools.read(ref) == content

    def test_list_prefix(self, tools):
        tools.capture("alpha", "1", run_id="run-a")
        tools.capture("beta", "2", run_id="run-b")
        assert tools.list("run-a/") == ["run-a/alpha.text"]
        assert "run-b/beta.text" in tools.list()

    def test_diff(self, tools):
        ref_a = tools.capture("cfg", "line1\nline2\nline3")
        ref_b = tools.capture("cfg", "line1\nline2-changed\nline3", run_id="run2")
        patch = tools.diff(ref_a, ref_b)
        assert "-line2" in patch
        assert "+line2-changed" in patch

    def test_read_missing_ref_raises(self, tools):
        with pytest.raises(FileNotFoundError):
            tools.read("ghost/none.text")

    def test_diff_missing_ref_raises(self, tools):
        ref = tools.capture("x", "x")
        with pytest.raises(FileNotFoundError):
            tools.diff(ref, "ghost/none.text")


class TestTrajectorySchema:
    def test_writer_reader_roundtrip(self, tmp_path):
        path = str(tmp_path / "traj.jsonl")
        writer = TrajectoryWriter(path)
        writer.write_event("run1", "start", node_id="n1")
        writer.write_event("run1", "tool_call", node_id="n1", payload={"tool": "rg"})
        writer.write_event("run1", "end", node_id="n1", status="success", duration_ms=120)
        records = TrajectoryReader(path).read_all()
        assert [r.event for r in records] == ["start", "tool_call", "end"]
        assert records[2].duration_ms == 120
        assert records[1].payload == {"tool": "rg"}

    def test_event_enum_enforced(self):
        with pytest.raises(TrajectorySchemaError):
            TrajectoryRecord(run_id="r", node_id="n", event="vibes")

    def test_node_or_member_required(self):
        with pytest.raises(TrajectorySchemaError):
            TrajectoryRecord(run_id="r", event="start")
        rec = TrajectoryRecord(run_id="r", member="worker1", event="start")
        assert rec.member == "worker1"

    def test_model_call_meta_privacy_rule(self):
        TrajectoryRecord(
            run_id="r",
            node_id="n",
            event="model_call_meta",
            payload={"model": "qwen3", "tokens_in": 120, "tokens_out": 40},
        )
        for banned_key in ("prompt", "messages", "content", "completion", "text"):
            with pytest.raises(TrajectorySchemaError):
                TrajectoryRecord(
                    run_id="r", node_id="n", event="model_call_meta", payload={banned_key: "x"}
                )

    def test_unknown_shape_rejected_loudly(self):
        with pytest.raises(TrajectorySchemaError):
            normalize_record({"totally": "unknown", "shape": 1})

    def test_reader_rejects_corrupt_line(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text("not-json\n", encoding="utf-8")
        with pytest.raises(TrajectorySchemaError):
            TrajectoryReader(str(path)).read_all()

    def test_events_contract(self):
        assert EVENTS == ("start", "tool_call", "tool_result", "model_call_meta", "error", "end")


class TestLegacyAdapters:
    def test_cowork_shaped_record_accepted(self):
        cowork_shape = {
            "ts": 1700000000.5,
            "event": "pre_node_execute",
            "execution_id": "exec-1",
            "workflow_id": "wf-1",
            "workflow_name": "scan",
            "session_id": "s-1",
            "trace_id": "t-1",
            "node_id": "fetch-node",
            "node_name": "fetch",
            "status": "running",
            "error": None,
            "is_error": False,
            "data": {"tool": "http"},
        }
        rec = normalize_record(cowork_shape)
        assert rec.run_id == "exec-1"
        assert rec.node_id == "fetch-node"
        assert rec.event == "start"
        assert rec.payload["legacy_event"] == "pre_node_execute"
        assert rec.payload["tool"] == "http"

    def test_cowork_error_event_maps(self):
        rec = normalize_record(
            {
                "ts": 1700000000.0,
                "event": "node_error",
                "execution_id": "exec-2",
                "node_id": "n",
                "status": "failed",
                "error": "boom",
                "is_error": True,
                "data": {},
            }
        )
        assert rec.event == "error"
        assert rec.status == "error"
        assert rec.payload["error"] == "boom"

    def test_studio_shaped_record_flattens(self):
        studio_shape = {
            "trace_id": "tr-9",
            "session_id": "s-9",
            "graph_id": "g-1",
            "graph_name": "pipeline",
            "agent_id": "agent-1",
            "started_at": 1700000000.0,
            "finished_at": 1700000005.0,
            "status": "completed",
            "events": [
                {"type": "start", "node_id": "step-1", "timestamp": 1700000000000},
                {
                    "type": "tool_call",
                    "node_id": "step-2",
                    "name": "rg",
                    "timestamp": 1700000001000,
                },
            ],
            "token_usage": {"input": 100, "output": 20},
        }
        recs = normalize_record(studio_shape)
        assert isinstance(recs, list) and len(recs) == 2
        assert recs[0].run_id == "tr-9"
        assert recs[0].event == "start"
        assert recs[1].event == "tool_call"
        assert recs[1].payload["name"] == "rg"
        assert recs[1].payload["run_token_usage"] == {"input": 100, "output": 20}

    def test_studio_think_maps_to_model_meta(self):
        studio_shape = {
            "trace_id": "tr-2",
            "session_id": "s-2",
            "events": [{"type": "think", "timestamp": 1}],
        }
        recs = normalize_record(studio_shape)
        assert recs[0].event == "model_call_meta"
        assert recs[0].payload["legacy_event"] == "think"

    def test_normative_shape_passthrough(self, tmp_path):
        path = str(tmp_path / "t.jsonl")
        writer = TrajectoryWriter(path)
        writer.write_event("run-z", "end", member="lead", status="success")
        rec = TrajectoryReader(path).read_all()[0]
        assert rec.run_id == "run-z"
        assert rec.status == "success"
