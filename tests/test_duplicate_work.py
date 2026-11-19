"""Issue #134: deterministic duplicate-work detection across TeamRuntime members."""

import pytest

from fusion_harness.funnels import LifecycleBus
from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.subagent import SubagentResult, SubagentRunner
from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import DUP_VALUE_CAP, TeamRuntime, TeamTask, _normalize_tool_key


class _ToolRunner(SubagentRunner):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def run(
        self,
        spec,
        agent_fn,
        parent_tools=None,
        parent_mode="default",
        event_sink=None,
        steer_queue=None,
    ):
        for event_type, payload in self.events:
            if event_sink is not None:
                event_sink(event_type, payload)
        return SubagentResult(subagent_id="x", name=spec.name, status="done", summary="ok")


def _definition():
    return TeamDefinition(
        name="scan-team",
        namespace_key="team:scan-2026-11",
        members=[TeamMember(name="m1", role="worker"), TeamMember(name="m2", role="worker")],
    )


def _runtime(threshold, bus=None, tmp_path=None, runner=None):
    return TeamRuntime(
        _definition(),
        runner=runner,
        bus=bus,
        state_dir=str(tmp_path / "sd") if tmp_path else None,
        duplicate_threshold=threshold,
    )


def _spy_bus():
    bus = LifecycleBus()
    events = []
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        events.append((event, payload))
        return orig(source, event, payload, session_id)

    bus.emit = spy
    return bus, events


TOOL_CALL = ("tool_call", {"call_id": "c1", "tool": "search_web", "args": {"query": "mlx kernel"}})


async def _run_members(rt, names=("m1", "m2")):
    rt.activate()
    task_id = rt.add_task(TeamTask(title="scan"))
    brief = SpawnBrief(
        objective="scan module X",
        output_format="json findings list",
        tool_guidance="use ripgrep",
        boundaries="read-only",
        token_budget=5000,
        concurrency=1,
        depth=1,
    )

    async def agent_fn(spec, session):
        return None

    for name in names:
        member = rt.definition.member(name)
        await rt.run_member(member, task_id, brief, agent_fn)
    return task_id


class TestDuplicateWork:
    async def test_off_by_default(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(None, bus=bus, tmp_path=tmp_path, runner=_ToolRunner([TOOL_CALL]))
        await _run_members(rt, names=("m1", "m1"))
        assert rt.duplicate_work_stats() == {}
        assert not [e for e in events if e[0] == "team_duplicate_work"]

    async def test_duplicate_across_members_emits_once(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(2, bus=bus, tmp_path=tmp_path, runner=_ToolRunner([TOOL_CALL]))
        task_id = await _run_members(rt)
        dup = [e for e in events if e[0] == "team_duplicate_work"]
        assert len(dup) == 1
        payload = dup[0][1]
        assert payload["task_id"] == task_id
        assert payload["members"] == ["m1", "m2"]
        assert payload["count"] == 2
        assert "search_web(" in payload["key"]

    async def test_same_member_repeat_no_emit(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(2, bus=bus, tmp_path=tmp_path, runner=_ToolRunner([TOOL_CALL]))
        await _run_members(rt, names=("m1", "m1"))
        assert not [e for e in events if e[0] == "team_duplicate_work"]
        stats = rt.duplicate_work_stats()
        assert next(iter(list(stats.values())[0].values()))["count"] == 2

    async def test_value_truncation_normalizes(self):
        long_a = "x" * 200 + "A"
        long_b = "x" * 200 + "B"
        assert _normalize_tool_key("t", {"q": long_a}) == _normalize_tool_key("t", {"q": long_b})
        assert len(_normalize_tool_key("t", {"q": long_a})) < 200
        assert DUP_VALUE_CAP == 120

    async def test_stats_exposure(self, tmp_path):
        rt = _runtime(2, tmp_path=tmp_path)
        rt._record_tool_call("t1", "m1", {"tool": "grep", "args": {"p": "a"}})
        rt._record_tool_call("t1", "m2", {"tool": "grep", "args": {"p": "a"}})
        rt._record_tool_call("t2", "m1", {"tool": "grep", "args": {"p": "a"}})
        stats = rt.duplicate_work_stats("t1")
        entry = stats["t1"]["grep(p=a)"]
        assert entry == {"count": 2, "members": ["m1", "m2"]}
        assert set(rt.duplicate_work_stats()) == {"t1", "t2"}

    def test_threshold_validation(self):
        with pytest.raises(ValueError):
            _runtime(1)

    async def test_detection_failure_never_breaks(self, tmp_path):
        rt = _runtime(2, tmp_path=tmp_path)
        rt._record_tool_call("t1", "m1", None)  # non-dict payload must be swallowed
        assert rt.duplicate_work_stats("t1") == {"t1": {}}
