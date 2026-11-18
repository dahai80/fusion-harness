"""Tests for task wakeup rules (issue #65): at/every kinds, blocked<->queued
transitions, end-timeout cancellation, cap, lazy prune, persistence."""

import pytest

from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import (
    WAKEUP_CAP_PER_TASK,
    TeamRuntime,
    TeamStateError,
    TeamTask,
)


def _definition(**kw):
    params = dict(
        name="wake-team",
        namespace_key="team:wake-2026-11",
        members=[TeamMember(name="worker1", role="worker")],
    )
    params.update(kw)
    return TeamDefinition(**params)


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _runtime(tmp_path, now=1000.0):
    clock = _Clock(now)
    rt = TeamRuntime(_definition(), state_dir=str(tmp_path), clock=clock)
    rt.activate()
    tid = rt.add_task(TeamTask(title="t"))
    return rt, tid, clock


def _blocked(rt, tid):
    rt.transition_task(tid, "in_progress")
    rt.transition_task(tid, "blocked", blocked_by=["x"])


class TestValidation:
    def test_bad_kind_rejected(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="kind must be"):
            rt.add_wakeup(tid, {"kind": "cron", "at": 2000.0})

    def test_at_requires_numeric_at(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="requires a numeric 'at'"):
            rt.add_wakeup(tid, {"kind": "at"})

    def test_every_requires_positive_every(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="positive numeric 'every'"):
            rt.add_wakeup(tid, {"kind": "every", "every": 0})
        with pytest.raises(TeamStateError, match="positive numeric 'every'"):
            rt.add_wakeup(tid, {"kind": "every"})

    def test_on_event_rejected_phase_2(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="phase 2"):
            rt.add_wakeup(tid, {"kind": "at", "at": 2000.0, "on_event": "done"})

    def test_bad_on_timeout_rejected(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="on_timeout must be"):
            rt.add_wakeup(tid, {"kind": "at", "at": 2000.0, "on_timeout": "yell"})

    def test_bad_expires_rejected(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="expires_at"):
            rt.add_wakeup(tid, {"kind": "at", "at": 2000.0, "expires_at": "soon"})


class TestCap:
    def test_cap_eight_armed_clean_error(self, tmp_path):
        rt, tid, _ = _runtime(tmp_path)
        for i in range(WAKEUP_CAP_PER_TASK):
            rt.add_wakeup(tid, {"kind": "at", "at": 5000.0 + i})
        with pytest.raises(TeamStateError, match="wakeup cap reached"):
            rt.add_wakeup(tid, {"kind": "at", "at": 6000.0})

    def test_fired_rules_free_cap_space(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        for i in range(WAKEUP_CAP_PER_TASK):
            rt.add_wakeup(tid, {"kind": "at", "at": 1000.5 + i})
        clock.now = 1000.6
        rt.check_wakeups(tid)
        rt.add_wakeup(tid, {"kind": "at", "at": 9000.0})


class TestFiring:
    def test_at_wakeup_moves_blocked_to_queued_exactly_once(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        _blocked(rt, tid)
        rt.add_wakeup(tid, {"kind": "at", "at": 1100.0})
        clock.now = 1100.0
        fired = rt.check_wakeups(tid)
        assert len(fired) == 1
        assert rt.task(tid).status == "queued"
        assert rt.check_wakeups(tid) == []
        assert rt.task(tid).status == "queued"

    def test_wakeup_on_non_blocked_task_no_transition(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        rt.transition_task(tid, "in_progress")
        rt.add_wakeup(tid, {"kind": "at", "at": 1100.0})
        clock.now = 1100.0
        fired = rt.check_wakeups(tid)
        assert len(fired) == 1
        assert rt.task(tid).status == "in_progress"

    def test_end_timeout_cancels_with_reason(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        _blocked(rt, tid)
        rt.add_wakeup(tid, {"kind": "at", "at": 1100.0, "on_timeout": "end"})
        clock.now = 1100.0
        rt.check_wakeups(tid)
        task = rt.task(tid)
        assert task.status == "cancelled"
        assert task.metadata["cancel_reason"] == "wakeup_timeout"

    def test_every_refires_on_interval(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        _blocked(rt, tid)
        rt.add_wakeup(tid, {"kind": "every", "every": 100.0})
        clock.now = 1100.0
        assert len(rt.check_wakeups(tid)) == 1
        assert rt.task(tid).status == "queued"
        rt.transition_task(tid, "in_progress")
        rt.transition_task(tid, "blocked", blocked_by=["y"])
        clock.now = 1150.0
        assert rt.check_wakeups(tid) == []
        clock.now = 1200.0
        assert len(rt.check_wakeups(tid)) == 1
        assert rt.task(tid).status == "queued"


class TestLazyPrune:
    def test_expired_wakeup_pruned_on_task_read(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        rt.add_wakeup(tid, {"kind": "at", "at": 1200.0, "expires_at": 1100.0})
        assert len(rt.task(tid).metadata["wakeups"]) == 1
        clock.now = 1100.0
        assert rt.task(tid).metadata["wakeups"] == []
        clock.now = 1200.0
        assert rt.check_wakeups(tid) == []

    def test_expired_wakeups_do_not_fire(self, tmp_path):
        rt, tid, clock = _runtime(tmp_path)
        _blocked(rt, tid)
        rt.add_wakeup(tid, {"kind": "at", "at": 1050.0, "expires_at": 1040.0})
        clock.now = 1100.0
        assert rt.check_wakeups(tid) == []
        assert rt.task(tid).status == "blocked"


class TestPersistence:
    def test_wakeups_survive_state_round_trip(self, tmp_path):
        import os

        rt, tid, clock = _runtime(tmp_path)
        _blocked(rt, tid)
        rt.add_wakeup(tid, {"kind": "at", "at": 1100.0})
        path = os.path.join(str(tmp_path), "state.json")
        rt.save_state(path)
        rt2 = TeamRuntime(_definition(), state_dir=str(tmp_path), clock=clock)
        rt2.activate()
        rt2.load_state(path)
        task = rt2.task(tid)
        assert task.status == "blocked"
        assert len(task.metadata["wakeups"]) == 1
        clock.now = 1100.0
        assert len(rt2.check_wakeups(tid)) == 1
        assert rt2.task(tid).status == "queued"

    def test_unknown_task_rejected(self, tmp_path):
        rt, _, _ = _runtime(tmp_path)
        with pytest.raises(TeamStateError, match="unknown task"):
            rt.add_wakeup("ghost-t1", {"kind": "at", "at": 2000.0})
