"""Tests for terminal-row invariant (#157)."""

import pytest

from fusion_harness import (
    HookEngine,
    LifecycleBus,
    TerminalRowTracker,
    wire_terminal_invariant,
)


def _spy_bus():
    bus = LifecycleBus()
    seen = []
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        seen.append((event, payload))
        return orig(source, event, payload, session_id=session_id)

    bus.emit = spy
    return bus, seen


def test_open_and_close_row():
    tracker = TerminalRowTracker()
    assert tracker.open_row("s1", "r1", kind="tool_call", turn_id="t1") is True
    assert len(tracker.open_rows("s1")) == 1
    assert tracker.close_row("s1", "r1") is True
    assert tracker.open_rows("s1") == []


def test_duplicate_open_ignored():
    tracker = TerminalRowTracker()
    assert tracker.open_row("s1", "r1") is True
    assert tracker.open_row("s1", "r1") is False
    assert len(tracker.open_rows("s1")) == 1


def test_close_unknown_row():
    tracker = TerminalRowTracker()
    assert tracker.close_row("s1", "ghost") is False


def test_terminalize_force_closes_open_rows():
    tracker = TerminalRowTracker()
    tracker.open_row("s1", "r1", kind="tool_call", turn_id="t1")
    tracker.open_row("s1", "r2", kind="llm_call", turn_id="t1")
    tracker.close_row("s1", "r1")
    forced = tracker.terminalize_turn("s1", turn_id="t1", outcome="failed")
    assert len(forced) == 1
    assert forced[0]["row_id"] == "r2"
    assert forced[0]["forced"] is True
    assert forced[0]["forced_outcome"] == "failed"
    assert tracker.open_rows("s1") == []


def test_terminalize_scoped_to_turn():
    tracker = TerminalRowTracker()
    tracker.open_row("s1", "r1", turn_id="t1")
    tracker.open_row("s1", "r2", turn_id="t2")
    forced = tracker.terminalize_turn("s1", turn_id="t1", outcome="cancelled")
    assert [row["row_id"] for row in forced] == ["r1"]
    assert [row["row_id"] for row in tracker.open_rows("s1")] == ["r2"]


def test_terminalize_clean_turn_emits_nothing():
    bus, seen = _spy_bus()
    tracker = TerminalRowTracker(bus=bus)
    tracker.open_row("s1", "r1")
    tracker.close_row("s1", "r1")
    assert tracker.terminalize_turn("s1", outcome="completed") == []
    assert [e for e, _ in seen if e == "terminal_row_violation"] == []


def test_terminalize_emits_violation_event():
    bus, seen = _spy_bus()
    tracker = TerminalRowTracker(bus=bus)
    tracker.open_row("s1", "r1", turn_id="t1")
    tracker.terminalize_turn("s1", turn_id="t1", outcome="failed")
    violations = [p for e, p in seen if e == "terminal_row_violation"]
    assert len(violations) == 1
    assert violations[0]["forced_rows"] == ["r1"]
    assert violations[0]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_wire_terminal_invariant_hook_fires():
    bus, seen = _spy_bus()
    tracker = TerminalRowTracker(bus=bus)
    tracker.open_row("s1", "r1", turn_id="t1")
    engine = HookEngine()
    wire_terminal_invariant(engine, tracker)
    result = await engine.fire("STOP", {"session_id": "s1", "turn_id": "t1"})
    assert result.decision != "block"
    assert tracker.open_rows("s1") == []
    violations = [p for e, p in seen if e == "terminal_row_violation"]
    assert len(violations) == 1


@pytest.mark.asyncio
async def test_invariant_hook_survives_tracker_error():
    tracker = TerminalRowTracker()
    engine = HookEngine()
    wire_terminal_invariant(engine, tracker)
    result = await engine.fire("STOP", {"session_id": None})
    assert result.decision != "block"


def test_sessions_isolated():
    tracker = TerminalRowTracker()
    tracker.open_row("s1", "r1")
    assert tracker.open_rows("s2") == []
    tracker.terminalize_turn("s2")
    assert len(tracker.open_rows("s1")) == 1
