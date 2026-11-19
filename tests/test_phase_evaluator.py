"""Issue #133: automatic evaluator checkpoint between ParallelTeam phases."""

from fusion_harness import LifecycleBus, ParallelTeam
from fusion_harness.subagent import SubagentResult, SubagentSpec


class _StubRunner:
    def __init__(self, fail=None):
        self.fail = fail or set()

    async def run(self, spec, agent_fn, parent_tools=None, parent_mode="default"):
        if spec.name in self.fail:
            return SubagentResult(subagent_id="x", name=spec.name, status="failed", error="boom")
        return SubagentResult(subagent_id="x", name=spec.name, status="done", summary="ok")


def _spec(name):
    return SubagentSpec(name=name, prompt="do x")


def _team(evaluator=None, bus=None, fail=None):
    return ParallelTeam(_StubRunner(fail=fail), phase_evaluator=evaluator, lifecycle_bus=bus)


def _spy_bus():
    bus = LifecycleBus()
    events = []
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        events.append((event, payload))
        return orig(source, event, payload, session_id)

    bus.emit = spy
    return bus, events


class TestPhaseEvaluator:
    async def test_no_evaluator_unchanged(self):
        team = _team()
        results = await team.run([_spec("a"), _spec("b")], None)
        assert all(r.status == "done" for r in results)

    async def test_passing_verdict_unchanged(self):
        team = _team(evaluator=lambda results: True)
        results = await team.run([_spec("a")], None)
        assert results[0].status == "done"

    async def test_failing_verdict_blocks(self):
        bus, events = _spy_bus()
        team = _team(evaluator=lambda results: False, bus=bus)
        results = await team.run([_spec("a"), _spec("b")], None)
        assert all(r.status == "blocked" for r in results)
        event = next(e for e in events if e[0] == "team_evaluator_block")
        assert event[1]["blocked"] == 2

    async def test_dict_verdict(self):
        bus, events = _spy_bus()
        team = _team(evaluator=lambda results: {"ok": False, "reason": "drifted off-plan"}, bus=bus)
        results = await team.run([_spec("a")], None)
        assert results[0].status == "blocked"
        event = next(e for e in events if e[0] == "team_evaluator_block")
        assert event[1]["verdict"] == "drifted off-plan"

    async def test_failed_members_not_touched(self):
        team = _team(evaluator=lambda r: False, fail={"bad"})
        results = await team.run([_spec("bad"), _spec("good")], None)
        statuses = {r.name: r.status for r in results}
        assert statuses == {"bad": "failed", "good": "blocked"}

    async def test_evaluator_error_fail_safe(self):
        bus, events = _spy_bus()

        def evaluator(results):
            raise RuntimeError("judge down")

        team = _team(evaluator=evaluator, bus=bus)
        results = await team.run([_spec("a")], None)
        assert results[0].status == "blocked"  # error = failing verdict, not raised
        event = next(e for e in events if e[0] == "team_evaluator_block")
        assert "judge down" in event[1]["verdict"]
