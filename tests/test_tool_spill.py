"""Tests for issue #94: tool-output spill-then-grep — oversized tool
results spill to the artifact store with grep-back reads, byte caps,
telemetry, and survival across checkpoint lineage forks."""

import pytest

from fusion_harness.artifact_channel import ArtifactStore
from fusion_harness.checkpoint import CheckpointState, CheckpointStore
from fusion_harness.tool_spill import (
    DEFAULT_READ_CAP_BYTES,
    SpillError,
    ToolOutputSpill,
)


@pytest.fixture
def spill(tmp_path):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    return ToolOutputSpill(store, max_in_context_bytes=1000, preview_bytes=50)


def _big(n=5000):
    return "\n".join("line %d: %s" % (i, "x" * 20) for i in range(n))


def test_small_output_passes_through(spill):
    result = spill.maybe_spill("run1", "read_file", "tiny output")
    assert result == {"spilled": False, "output": "tiny output"}
    assert spill.run_telemetry()["spill_count"] == 0


def test_oversized_output_spills_with_preview(spill):
    out = _big()
    result = spill.maybe_spill("run1", "read_file", out)
    assert result["spilled"] is True
    assert result["bytes"] >= 1000
    assert len(result["head"]) <= 50
    assert result["tail"]
    assert "ref" in result


def test_spill_grep_roundtrip(spill):
    lines = ["alpha result here " + "z" * 120 for _ in range(10)]
    lines[5] = "the needle is here " + "z" * 120
    result = spill.maybe_spill("run1", "grep_target", "\n".join(lines))
    hits = spill.grep(result["ref"], "needle")
    assert len(hits) == 1
    assert hits[0]["line"] == 6
    assert "needle is here" in hits[0]["text"]


def test_grep_result_cap(spill):
    out = "match me\n" * 500
    result = spill.maybe_spill("run1", "cap", out)
    hits = spill.grep(result["ref"], "match me", max_results=10)
    assert len(hits) == 10


def test_read_byte_cap_enforced(spill):
    result = spill.maybe_spill("run1", "capread", "A" * 10000)
    window = spill.read(result["ref"], offset=0, max_bytes=100)
    assert len(window) == 100
    # Asking for more than the hard cap is clamped, not honored.
    window = spill.read(result["ref"], offset=0, max_bytes=10**9)
    assert len(window) == DEFAULT_READ_CAP_BYTES


def test_read_invalid_window_raises(spill):
    result = spill.maybe_spill("run1", "x", "A" * 5000)
    with pytest.raises(SpillError):
        spill.read(result["ref"], offset=-1)


def test_handle_traversal_refused(spill, tmp_path):
    evil = "../outside.txt"
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(SpillError):
        spill.grep(evil, "secret")
    with pytest.raises(SpillError):
        spill.read(evil)


def test_unknown_handle_raises(spill):
    with pytest.raises(SpillError):
        spill.read("nonexistent/run.text")


def test_telemetry_per_run(spill):
    spill.maybe_spill("run1", "t1", "A" * 5000)
    spill.maybe_spill("run1", "t2", "B" * 5000)
    spill.maybe_spill("run2", "t3", "C" * 5000)
    t1 = spill.run_telemetry("run1")
    assert t1["spill_count"] == 2
    assert t1["bytes_spilled"] == 10000
    assert spill.run_telemetry("run2")["spill_count"] == 1


def test_spilled_handle_survives_checkpoint_fork(spill, tmp_path):
    store = CheckpointStore(str(tmp_path / "ckpt.db"))
    state = CheckpointState(messages=[{"role": "user", "content": "task"}])
    store.save("sess-a", state)
    result = spill.maybe_spill("sess-a", "big_tool", "D" * 5000)
    child = store.fork("sess-a", 1, new_session_id="sess-b")
    assert child["session_id"] == "sess-b"
    # The spilled artifact is on disk in the run-scoped store — the fork's
    # context still resolves the handle.
    window = spill.read(result["ref"], max_bytes=64)
    assert window.startswith("DDDD")
    hits = spill.grep(result["ref"], "D+")
    assert hits


def test_spill_write_failure_degrades_to_passthrough(spill, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(spill.store, "write_artifact", boom)
    result = spill.maybe_spill("run1", "t", "E" * 5000)
    assert result["spilled"] is False
    assert "spill_error" in result
