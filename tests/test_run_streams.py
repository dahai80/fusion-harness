"""Tests for per-run event streams (issue #63): one JSONL per team member
run at <state_dir>/runs/<task_id>/<attempt>.jsonl, redaction, usage."""

import asyncio
import json
import os

from fusion_harness.session_log import SessionEventLog
from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import TeamRuntime, TeamTask, _redact


def _member(name, **kw):
    return TeamMember(name=name, role="worker", **kw)


def _definition(**kw):
    params = dict(
        name="stream-team",
        namespace_key="team:stream-2026-11",
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
    return rt


def _run(rt, tid, member, agent_fn=None):
    return asyncio.run(rt.run_member(member, tid, _brief(), agent_fn))


def _stream_path(tmp_path, task_id, attempt):
    return os.path.join(str(tmp_path), "runs", task_id, "%d.jsonl" % attempt)


def _read_events(tmp_path, task_id, attempt):
    path = _stream_path(tmp_path, task_id, attempt)
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestRunStreamShape:
    def test_one_jsonl_per_run_with_expected_events(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))
        _run(rt, tid, rt.definition.member("worker1"))
        events = _read_events(tmp_path, tid, 1)
        types = [e["type"] for e in events]
        assert types[0] == "run_started"
        assert "prompt" in types
        assert types[-2:] == ["final", "run_finished"]
        assert events[0]["payload"]["task_id"] == tid
        assert events[0]["payload"]["attempt"] == 1

    def test_prompt_event_carries_assembled_prompt(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))
        _run(rt, tid, rt.definition.member("worker1"))
        events = _read_events(tmp_path, tid, 1)
        prompt_event = next(e for e in events if e["type"] == "prompt")
        assert prompt_event["payload"]["text"].endswith("do the thing")

    def test_attempt_increments_per_run(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))
        m = rt.definition.member("worker1")
        _run(rt, tid, m)
        tid2 = rt.add_task(TeamTask(title="t3"))
        _run(rt, tid, m)
        _run(rt, tid2, m)
        assert os.path.exists(_stream_path(tmp_path, tid, 2))
        assert os.path.exists(_stream_path(tmp_path, tid2, 1))

    def test_tool_events_replay_from_jsonl(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))

        async def agent_fn(spec, session):
            session.log_event("tool_call", {"call_id": "c1", "tool": "search_web"})
            session.log_event("tool_result", {"call_id": "c1", "bytes": 12})
            session.record_turn(tokens=7)
            return "all done"

        result = _run(rt, tid, rt.definition.member("worker1"), agent_fn)
        assert result.status == "completed"
        log = SessionEventLog.load(_stream_path(tmp_path, tid, 1))
        replayed = log.replay()
        assert replayed.completed_tool_calls[0]["call_id"] == "c1"
        assert replayed.completed_tool_calls[0]["ok"] is True


class TestFailedRunReplay:
    def test_failed_run_replays_including_tool_error(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))

        async def agent_fn(spec, session):
            session.log_event("tool_call", {"call_id": "c2", "tool": "shell"})
            session.log_event("tool_error", {"call_id": "c2", "error": "boom"})
            raise RuntimeError("member exploded")

        result = _run(rt, tid, rt.definition.member("worker1"), agent_fn)
        assert result.status == "failed"
        events = _read_events(tmp_path, tid, 1)
        types = [e["type"] for e in events]
        assert "tool_error" in types
        assert types[-1] == "run_finished"
        assert events[-1]["payload"]["status"] == "failed"
        log = SessionEventLog.load(_stream_path(tmp_path, tid, 1))
        state = log.replay()
        assert state.completed_tool_calls[0]["ok"] is False
        assert state.completed_tool_calls[0]["outcome"]["error"] == "boom"


class TestRunUsage:
    def test_run_finished_usage_matches_session_totals(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))

        async def agent_fn(spec, session):
            session.record_turn(tokens=42)
            session.record_turn(tokens=13)
            return "ok"

        _run(rt, tid, rt.definition.member("worker1"), agent_fn)
        events = _read_events(tmp_path, tid, 1)
        usage = events[-1]["payload"]["usage"]
        assert usage["total_tokens"] == 55

    def test_usage_with_ledger_breakdown(self, tmp_path):
        class FakeLedger:
            def balance(self, scope=None):
                return {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "reasoning_tokens": 5,
                    "total_tokens": 120,
                }

        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))

        async def agent_fn(spec, session):
            session.ledger = FakeLedger()
            return "ok"

        _run(rt, tid, rt.definition.member("worker1"), agent_fn)
        usage = _read_events(tmp_path, tid, 1)[-1]["payload"]["usage"]
        assert usage == {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "reasoning_tokens": 5,
            "total_tokens": 120,
        }


class TestRedaction:
    def test_embedded_secret_in_command_redacted(self):
        out = _redact({"command": "export API_KEY=sk-live-abcd1234 && run"})
        assert "sk-live-abcd1234" not in out["command"]
        assert "API_KEY=***" in out["command"]

    def test_sensitive_key_redacted(self):
        assert _redact({"api_key": "sk-123", "tool": "http"})["api_key"] == "***"
        assert _redact({"Authorization": "Bearer xyz"})["Authorization"] == "***"

    def test_nested_structures_redacted(self):
        out = _redact({"args": {"password": "hunter2", "n": 1}, "list": [{"token": "t"}]})
        assert out["args"]["password"] == "***"
        assert out["list"][0]["token"] == "***"

    def test_redaction_applied_on_stream_write(self, tmp_path):
        rt = _runtime(tmp_path)
        tid = rt.add_task(TeamTask(title="t"))

        async def agent_fn(spec, session):
            session.log_event("tool_call", {"call_id": "c3", "command": "TOKEN=abcdef123 run"})
            return "ok"

        _run(rt, tid, rt.definition.member("worker1"), agent_fn)
        raw = open(_stream_path(tmp_path, tid, 1), encoding="utf-8").read()
        assert "abcdef123" not in raw
        assert "TOKEN=***" in raw


class TestUnaffectedPaths:
    def test_plain_subagent_spawn_no_sink_no_stream(self, tmp_path):
        from fusion_harness.subagent import SubagentRunner, SubagentSpec

        rt = _runtime(tmp_path)
        runner = SubagentRunner(bus=rt.bus)

        async def agent_fn(spec, session):
            session.log_event("tool_call", {"tool": "x"})
            return "solo done"

        result = asyncio.run(runner.run(SubagentSpec(name="solo", prompt="p"), agent_fn))
        assert result.status == "completed"
        assert not os.path.exists(os.path.join(str(tmp_path), "runs"))

    def test_session_log_event_noop_without_sink(self):
        from fusion_harness.subagent import SubagentRunner, SubagentSpec

        runner = SubagentRunner()

        async def agent_fn(spec, session):
            session.log_event("tool_call", {"tool": "x"})
            return "ok"

        result = asyncio.run(runner.run(SubagentSpec(name="s", prompt="p"), agent_fn))
        assert result.status == "completed"
