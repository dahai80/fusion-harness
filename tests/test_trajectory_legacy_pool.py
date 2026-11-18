"""Tests for issue #81: TrajectoryReader tolerates legacy cowork records
with empty node_id (workflow-level events) and offers opt-in skip semantics
for mixed pools."""

import json

import pytest

from fusion_harness.trajectory import TrajectoryReader, TrajectorySchemaError

LEGACY_LINE = json.dumps(
    {
        "ts": 1700000000.0,
        "event": "workflow_start",
        "execution_id": "exec_1",
        "workflow_id": "wf_1",
        "workflow_name": "demo",
        "session_id": "s1",
        "trace_id": "",
        "node_id": "",
        "node_name": "",
        "status": "",
        "error": None,
        "is_error": False,
        "data": {},
    }
)


def _pool(tmp_path, lines):
    path = tmp_path / "pool.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class TestLegacyNodeScope:
    def test_legacy_workflow_line_reads(self, tmp_path):
        path = _pool(tmp_path, [LEGACY_LINE])
        records = TrajectoryReader(path).read_all()
        assert len(records) == 1
        assert records[0].node_id == "workflow"
        assert records[0].event == "start" or records[0].event

    def test_mixed_pool_reads(self, tmp_path):
        good = json.dumps(
            {
                "ts": 1700000001.0,
                "event": "task_step",
                "execution_id": "exec_1",
                "node_id": "n1",
                "data": {},
            }
        )
        path = _pool(tmp_path, [LEGACY_LINE, good])
        records = TrajectoryReader(path).read_all()
        assert [r.node_id for r in records] == ["workflow", "n1"]

    def test_member_fallback_before_workflow(self, tmp_path):
        line = json.dumps(
            {
                "ts": 1700000002.0,
                "event": "task_step",
                "execution_id": "e2",
                "node_id": "",
                "member": "beta",
                "data": {},
            }
        )
        path = _pool(tmp_path, [line])
        records = TrajectoryReader(path).read_all()
        assert records[0].node_id == "beta"


class TestSkipSemantics:
    def test_strict_default_still_raises(self, tmp_path):
        path = _pool(tmp_path, ["not-json", LEGACY_LINE])
        with pytest.raises(TrajectorySchemaError):
            TrajectoryReader(path).read_all()

    def test_skip_corrupt_skips_bad_line(self, tmp_path):
        path = _pool(tmp_path, ["not-json", LEGACY_LINE])
        reader = TrajectoryReader(path, skip_corrupt=True)
        records = reader.read_all()
        assert len(records) == 1
        assert reader.skipped == 1

    def test_skip_unknown_shape(self, tmp_path):
        path = _pool(tmp_path, ['{"totally": "unknown", "shape": 1}', LEGACY_LINE])
        reader = TrajectoryReader(path, skip_corrupt=True)
        records = reader.read_all()
        assert len(records) == 1
        assert reader.skipped == 1
