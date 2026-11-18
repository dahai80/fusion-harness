"""Tests for mid-run steering (issue #64): supplement queue attached in
run_member, drained at node boundaries, notes recorded to the run stream."""

import asyncio
import os

from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import TeamRuntime, TeamTask


def _member(name, **kw):
    return TeamMember(name=name, role="worker", **kw)


def _definition(**kw):
    params = dict(
        name="steer-team",
        namespace_key="team:steer-2026-11",
        members=[_member("worker1")],
    )
    params.update(kw)
    return TeamDefinition(**params)


def _brief():
    return SpawnBrief(
        objective="do the thing",
        output_format="text",
        tool_guidance="t",
        boundaries="b",
        token_budget=5000,
        concurrency=1,
        depth=1,
    )


def _runtime(tmp_path):
    rt = TeamRuntime(_definition(), state_dir=str(tmp_path))
    rt.activate()
    tid = rt.add_task(TeamTask(title="t"))
    return rt, tid


class TestSteerMember:
    def test_steer_without_active_run_returns_false(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        assert rt.steer_member(tid, "focus on X") is False

    def test_empty_content_rejected(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        try:
            rt.steer_member(tid, "   ")
        except ValueError as exc:
            assert "steer content required" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_bad_origin_rejected(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        try:
            rt.steer_member(tid, "x", origin="ghost")
        except ValueError as exc:
            assert "origin" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_no_harness_side_cap(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        ready = asyncio.Event()

        async def agent_fn(spec, session):
            ready.set()
            await asyncio.sleep(0.05)
            notes = session.drain_steers()
            return "got %d" % len(notes)

        async def scenario():
            task = asyncio.ensure_future(
                rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
            )
            await ready.wait()
            for i in range(5):
                assert rt.steer_member(tid, "note %d" % i) is True
            return await task

        result = asyncio.run(scenario())
        assert result.summary == "got 5"


class TestMidRunInjection:
    def test_supplement_changes_output_within_same_run(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        ready = asyncio.Event()

        async def agent_fn(spec, session):
            ready.set()
            await asyncio.sleep(0.05)
            notes = session.drain_steers()
            if not notes:
                return "no steer"
            return "adjusted: %s" % notes[0]

        async def scenario():
            task = asyncio.ensure_future(
                rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
            )
            await ready.wait()
            assert rt.steer_member(tid, "skip the slow path") is True
            return await task

        result = asyncio.run(scenario())
        assert result.summary == "adjusted: [steer] operator note: skip the slow path"

    def test_drain_between_tool_calls_only_agent_discipline(self, tmp_path):
        """Harness never injects mid-tool-call: injection points are the
        drain_steers() calls themselves — a tool call/result pair logged
        before the drain stays contiguous in the stream."""
        rt, tid = _runtime(tmp_path)
        ready = asyncio.Event()

        async def agent_fn(spec, session):
            ready.set()
            await asyncio.sleep(0.05)
            session.log_event("tool_call", {"call_id": "c1"})
            session.log_event("tool_result", {"call_id": "c1"})
            notes = session.drain_steers()
            session.log_event("tool_call", {"call_id": "c2"})
            session.log_event("tool_result", {"call_id": "c2"})
            return "steered" if notes else "plain"

        async def scenario():
            task = asyncio.ensure_future(
                rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
            )
            await ready.wait()
            rt.steer_member(tid, "after c1")
            return await task

        result = asyncio.run(scenario())
        assert result.summary == "steered"

    def test_steer_note_recorded_in_run_stream(self, tmp_path):
        import json

        rt, tid = _runtime(tmp_path)
        ready = asyncio.Event()

        async def agent_fn(spec, session):
            ready.set()
            await asyncio.sleep(0.05)
            session.drain_steers()
            return "done"

        async def scenario():
            task = asyncio.ensure_future(
                rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
            )
            await ready.wait()
            rt.steer_member(tid, "write this down")
            return await task

        asyncio.run(scenario())
        path = os.path.join(str(tmp_path), "runs", tid, "1.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            types = [json.loads(line)["type"] for line in fh if line.strip()]
        assert "steer" in types

    def test_lead_origin_format(self, tmp_path):
        rt, tid = _runtime(tmp_path)
        ready = asyncio.Event()

        async def agent_fn(spec, session):
            ready.set()
            await asyncio.sleep(0.05)
            notes = session.drain_steers()
            return notes[0] if notes else "none"

        async def scenario():
            task = asyncio.ensure_future(
                rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
            )
            await ready.wait()
            rt.steer_member(tid, "board says done", origin="lead")
            return await task

        result = asyncio.run(scenario())
        assert result.summary == "[steer] lead note: board says done"

    def test_queue_detached_after_run(self, tmp_path):
        rt, tid = _runtime(tmp_path)

        async def agent_fn(spec, session):
            return "done"

        asyncio.run(rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn))
        assert tid not in rt._supplement_queues
        assert rt.steer_member(tid, "late") is False


class TestUnaffectedPaths:
    def test_plain_subagent_spawn_drain_noop(self):
        from fusion_harness.subagent import SubagentRunner, SubagentSpec

        runner = SubagentRunner()

        async def agent_fn(spec, session):
            assert session.drain_steers() == []
            return "ok"

        result = asyncio.run(runner.run(SubagentSpec(name="s", prompt="p"), agent_fn))
        assert result.status == "completed"
