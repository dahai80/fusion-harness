"""Tests for cross-session experience layer (#164)."""

from fusion_harness import (
    NOTEBOOK_MULTIPLIER,
    LifecycleBus,
    LoopGuard,
    PruneProposal,
    SpiralState,
    ToolPruner,
    is_notebook_tool,
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


# ---------- SpiralState ----------


def test_spiral_state_folds_edits_and_failures():
    state = SpiralState()
    state.fold("edit", file_edits=("a.py",), error_output="")
    state.fold("edit", file_edits=("a.py", "b.py"), error_output="")
    state.fold("shell", file_edits=("a.py",), error_output="ModuleNotFoundError: x")
    assert state.edits_per_file["a.py"] == 3
    assert len(state.files_edited) == 2
    assert any("ModuleNotFoundError" in sig for sig in state.repeat_failures)


def test_spiral_state_edit_spiral_detection():
    state = SpiralState()
    for i in range(4):
        state.fold("edit", file_edits=("f%d.py" % i,))
    assert state.is_edit_spiral(min_files=4, repeats_per_file=3) is False
    state.fold("edit", file_edits=("f0.py",))
    assert state.is_edit_spiral(min_files=4, repeats_per_file=3) is False
    state.fold("edit", file_edits=("f0.py",))
    assert state.is_edit_spiral(min_files=4, repeats_per_file=3) is True


def test_spiral_state_reset_segment():
    state = SpiralState()
    state.fold("edit", file_edits=("a.py",))
    state.reset_segment()
    assert state.calls_in_segment == 0
    assert state.edits_per_file["a.py"] == 1


# ---------- LoopGuard ----------


def test_guard_passes_healthy_calls():
    guard = LoopGuard()
    for i in range(5):
        verdict = guard.evaluate(
            "edit",
            file_edits=("f%d_a.py" % i, "f%d_b.py" % i, "f%d_c.py" % i, "f%d_d.py" % i),
        )
        assert verdict.action == "pass"


def test_guard_redirect_on_repeat_failures():
    guard = LoopGuard(failure_threshold=3)
    for _ in range(2):
        guard.evaluate("shell", error_output="connection refused to host x")
    verdict = guard.evaluate("shell", error_output="connection refused to host x")
    assert verdict.action == "redirect"
    assert "repeat failure" in verdict.reason
    assert verdict.action != "abort"


def test_guard_second_stuck_verdict_is_notice():
    guard = LoopGuard(failure_threshold=2)
    guard.evaluate("shell", error_output="boom alpha")
    assert guard.evaluate("shell", error_output="boom alpha").action == "redirect"
    guard.evaluate("shell", error_output="boom alpha")
    assert guard.evaluate("shell", error_output="boom alpha").action == "notice"


def test_guard_edit_spiral_redirect():
    guard = LoopGuard(spiral_min_files=4, spiral_repeats=3)
    for i in range(4):
        guard.evaluate("edit", file_edits=("f%d.py" % i,))
    guard.evaluate("edit", file_edits=("f0.py",))
    verdict = guard.evaluate("edit", file_edits=("f0.py",))
    assert verdict.action == "redirect"
    assert "edit spiral" in verdict.reason


def test_guard_envelope_threshold_adaptive():
    guard = LoopGuard(min_turn_samples=5, envelope_pct=90)
    for length in [4, 5, 6, 7, 8]:
        guard.record_turn_end(length, ended_on_own=True, failed_verification_after_edit=False)
    assert guard.envelope_threshold() == 8.0


def test_guard_envelope_ignores_failed_verification_turns():
    guard = LoopGuard(min_turn_samples=3)
    guard.record_turn_end(50, ended_on_own=True, failed_verification_after_edit=True)
    guard.record_turn_end(50, ended_on_own=False, failed_verification_after_edit=False)
    assert guard.envelope_threshold() == 0.0


def test_guard_envelope_exceeded_after_enough_samples():
    guard = LoopGuard(min_turn_samples=3, envelope_pct=90)
    for length in [2, 2, 2]:
        guard.record_turn_end(length, ended_on_own=True, failed_verification_after_edit=False)
    verdicts = [guard.evaluate("edit", file_edits=("a.py",)) for _ in range(4)]
    assert any(v.envelope_exceeded for v in verdicts)


def test_guard_fail_open_on_internal_error():
    guard = LoopGuard()
    guard.state = None
    verdict = guard.evaluate("edit")
    assert verdict.action == "pass"
    assert "fail-open" in verdict.reason


def test_guard_optional_judge_called_and_emitted():
    bus, seen = _spy_bus()
    calls = []

    def judge(reason):
        calls.append(reason)
        return "try the other module"

    guard = LoopGuard(bus=bus, failure_threshold=2, judge=judge)
    guard.evaluate("shell", error_output="err one")
    guard.evaluate("shell", error_output="err one")
    assert len(calls) == 1
    judge_events = [p for e, p in seen if e == "loop_guard_judge"]
    assert len(judge_events) == 1
    assert judge_events[0]["note"] == "try the other module"


def test_guard_emits_redirect_event():
    bus, seen = _spy_bus()
    guard = LoopGuard(bus=bus, failure_threshold=1)
    guard.evaluate("shell", error_output="bad")
    assert [e for e, _ in seen if e == "loop_guard_redirect"]


# ---------- ToolPruner ----------


def _pruner_with_sessions():
    pruner = ToolPruner()
    for i, tools in enumerate(
        [
            ["shell", "read", "quantum_flux", "mcp__ghost__summon"],
            ["shell", "read", "quantum_flux"],
            ["shell", "read", "quantum_flux"],
        ]
    ):
        sid = "s%d" % i
        pruner.load_session(sid, tools)
        for _ in range(6):
            pruner.record_prompt(sid)
        pruner.record_use(sid, "shell")
        pruner.record_use(sid, "read")
    return pruner


def test_pruner_proposes_unused_tool():
    pruner = _pruner_with_sessions()
    proposals = pruner.propose_prunes(min_prompts=5, min_sessions=2, idle_sessions=3)
    names = [p.tool_name for p in proposals]
    assert "quantum_flux" in names
    assert "shell" not in names
    assert "read" not in names


def test_pruner_protected_tools_never_proposed():
    pruner = ToolPruner()
    pruner.load_session("s1", ["shell", "read"])
    for _ in range(10):
        pruner.record_prompt("s1")
    assert pruner.propose_prunes(min_prompts=1, min_sessions=1, idle_sessions=1) == []


def test_pruner_one_use_resets_streak():
    pruner = ToolPruner()
    pruner.load_session("s1", ["quantum_flux"])
    for _ in range(6):
        pruner.record_prompt("s1")
    pruner.record_use("s1", "quantum_flux")
    for _ in range(6):
        pruner.record_prompt("s1")
    proposals = pruner.propose_prunes(min_prompts=5, min_sessions=2, idle_sessions=1)
    assert [p.tool_name for p in proposals] == []


def test_pruner_notebook_needs_longer_streak():
    pruner = ToolPruner(protected_tools=())
    pruner.load_session("s1", ["notebook_edit", "quantum_flux"])
    for _ in range(6):
        pruner.record_prompt("s1")
    proposals = pruner.propose_prunes(min_prompts=5, min_sessions=1, idle_sessions=1)
    names = [p.tool_name for p in proposals]
    assert "quantum_flux" in names
    assert "notebook_edit" not in names
    for _ in range(9):
        pruner.record_prompt("s1")
    proposals = pruner.propose_prunes(min_prompts=5, min_sessions=1, idle_sessions=1)
    names = [p.tool_name for p in proposals]
    assert "notebook_edit" in names


def test_pruner_notebook_multiplier_constant():
    assert NOTEBOOK_MULTIPLIER == 3.0
    assert is_notebook_tool("NotebookEdit") is True
    assert is_notebook_tool("read") is False


def test_pruner_idle_sessions_rule_any_length():
    pruner = ToolPruner()
    pruner.load_session("s1", ["quantum_flux"])
    pruner.record_prompt("s1")
    pruner.load_session("s2", ["quantum_flux"])
    pruner.load_session("s3", ["quantum_flux"])
    proposals = pruner.propose_prunes(min_prompts=100, min_sessions=100, idle_sessions=3)
    assert [p.tool_name for p in proposals] == ["quantum_flux"]


def test_pruner_proposal_is_evidence_backed():
    pruner = _pruner_with_sessions()
    proposal = pruner.propose_prunes(min_prompts=5, min_sessions=2, idle_sessions=3)[0]
    assert isinstance(proposal, PruneProposal)
    assert proposal.evidence["unused_sessions"] >= 3
    assert "unused" in proposal.reason


def test_pruner_restore_is_reversible_noop_ledger():
    bus, seen = _spy_bus()
    pruner = ToolPruner(bus=bus)
    pruner.restore("quantum_flux")
    assert [e for e, _ in seen if e == "tool_prune_restored"]
