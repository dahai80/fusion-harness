"""Team entity tests (issue #31, T1): TeamDefinition validation,
TeamRuntime lifecycle/task-board/mailbox/persistence, zero-model-call CI check."""

import json
import os

import pytest

from fusion_harness.funnels import LifecycleBus
from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.subagent import SubagentResult
from fusion_harness.team import TeamDefError, TeamDefinition, TeamMember
from fusion_harness.team_runtime import (
    MSG_TYPES,
    TeamMessage,
    TeamMessageError,
    TeamRuntime,
    TeamStateError,
    TeamTask,
)


def _member(name, **kw):
    return TeamMember(name=name, role="worker", **kw)


def _definition(**kw):
    params = dict(
        name="scan-team",
        namespace_key="team:scan-2026-11",
        members=[_member("lead"), _member("worker1"), _member("worker2")],
    )
    params.update(kw)
    return TeamDefinition(**params)


def _brief(token_budget=5000):
    return SpawnBrief(
        objective="scan module X",
        output_format="json findings list",
        tool_guidance="use ripgrep",
        boundaries="read-only",
        token_budget=token_budget,
        concurrency=1,
        depth=1,
    )


class TestTeamDefinition:
    def test_valid_definition(self):
        d = _definition()
        assert d.memory_anchor == {"tier": "team", "key": "team:scan-2026-11"}

    def test_duplicate_names_rejected(self):
        with pytest.raises(TeamDefError):
            TeamDefinition(name="t", namespace_key="k", members=[_member("a"), _member("a")])

    def test_empty_namespace_rejected(self):
        with pytest.raises(TeamDefError):
            TeamDefinition(name="t", namespace_key="", members=[_member("a")])

    def test_depth_overflow_rejected(self):
        with pytest.raises(TeamDefError):
            _definition(max_depth=4)

    def test_member_budget_over_lead_rejected(self):
        with pytest.raises(TeamDefError):
            _definition(lead_budget_tokens=1000, members=[_member("a", budget_tokens=5000)])


class TestLifecycle:
    def test_legal_path(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        assert rt.lifecycle == "DEFINED"
        rt.activate()
        rt.pause()
        rt.resume()
        rt.disband()
        assert rt.lifecycle == "DISBANDED"

    def test_illegal_transitions_raise_typed(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        with pytest.raises(TeamStateError):
            rt.pause()
        rt.activate()
        rt.disband()
        with pytest.raises(TeamStateError):
            rt.resume()


def temp_dir():
    import tempfile

    return tempfile.mkdtemp(prefix="team-rt-")


class TestTaskBoard:
    def test_whitelist_transitions(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="scan", detail="d"))
        assert tid == "scan-team-t1"
        rt.transition_task(tid, "in_progress")
        rt.transition_task(tid, "done")
        assert rt.task(tid).status == "done"

    def test_illegal_transition_typed_error(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="t"))
        with pytest.raises(TeamStateError):
            rt.transition_task(tid, "done")

    def test_blocked_requires_blocked_by(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="t"))
        with pytest.raises(TeamStateError):
            rt.transition_task(tid, "blocked", blocked_by=[])
        rt.transition_task(tid, "blocked", blocked_by=["dep-x"])
        assert rt.task(tid).blocked_by == ["dep-x"]

    def test_claim_is_atomic_single_winner(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="t"))
        assert rt.claim_task(tid, "worker1") is True
        assert rt.claim_task(tid, "worker2") is False
        assert rt.task(tid).assignee == "worker1"


class TestMemberExecution:
    def test_constraints_stack_only_tighten(self):
        rt = TeamRuntime(
            _definition(
                members=[_member("w", tool_allowlist=["read_file"], permission_mode="plan")]
            ),
            state_dir=temp_dir(),
        )
        rt.activate()
        member = rt.definition.member("w")
        tid = rt.add_task(TeamTask(title="t"))
        captured = {}

        async def fake_run(spec, agent_fn):
            captured["spec"] = spec
            return SubagentResult(
                subagent_id="s1", name=member.name, status="completed", summary="done"
            )

        rt.runner.run = fake_run
        import asyncio

        result = asyncio.run(rt.run_member(member, tid, _brief(token_budget=1000), None))
        assert result.status == "completed"
        spec = captured["spec"]
        assert spec.tool_allowlist == ["read_file"]
        assert spec.permission_mode == "plan"
        assert spec.budget_tokens == 1000
        board = rt.task(tid).metadata["results"]
        assert board[0]["summary"] == "done"

    def test_depth_overflow_rejected_at_runtime(self):
        rt = TeamRuntime(_definition(max_depth=2), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="t"))
        import asyncio

        with pytest.raises(TeamStateError):
            asyncio.run(rt.run_member(rt.definition.member("lead"), tid, _brief(), None, depth=2))
        asyncio.run(rt.run_member(rt.definition.member("lead"), tid, _brief(), None, depth=1))

    def test_member_failure_lands_on_board_not_raised(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        tid = rt.add_task(TeamTask(title="t"))

        async def boom(spec, agent_fn):
            raise RuntimeError("child exploded")

        import asyncio

        result = asyncio.run(rt.run_member(rt.definition.member("worker1"), tid, _brief(), boom))
        assert result.status == "failed"
        assert rt.task(tid).metadata["results"][0]["status"] == "failed"


class TestMailbox:
    def test_send_delivered_only_on_write_success(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        ok = rt.send(TeamMessage(type="text", sender="lead", recipient="worker1", body="hi"))
        assert ok is True
        assert rt.inbox("worker1")[0]["body"] == "hi"

    def test_send_failure_returns_false(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        rt._inbox_path = lambda name: "/proc/nonexistent-dir/x.json"
        ok = rt.send(TeamMessage(type="text", sender="lead", recipient="worker1", body="hi"))
        assert ok is False
        assert rt.inbox("worker1") == []

    def test_message_type_enum_validated(self):
        with pytest.raises(TeamMessageError):
            TeamMessage(type="gossip", sender="lead", recipient="worker1")
        assert set(MSG_TYPES) == {"text", "plan_approval", "shutdown_request", "shutdown_response"}

    def test_broadcast_one_delivery_per_member(self):
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        assert rt.broadcast("lead", "standup") == 2
        assert len(rt.inbox("worker1")) == 1
        assert len(rt.inbox("worker2")) == 1
        assert rt.inbox("lead") == []


class TestPersistence:
    def test_roundtrip_reproduces_board_and_inboxes(self):
        state_dir = temp_dir()
        rt = TeamRuntime(_definition(), state_dir=state_dir)
        rt.activate()
        tid = rt.add_task(TeamTask(title="t1", detail="d"))
        rt.transition_task(tid, "in_progress")
        rt.claim_task(tid, "worker1")
        rt.send(TeamMessage(type="text", sender="lead", recipient="worker2", body="m1"))
        path = os.path.join(state_dir, "state.json")
        rt.save_state(path)
        before_board = {k: v.status for k, v in rt._tasks.items()}
        before_inbox = rt.inbox("worker2")

        rt2 = TeamRuntime(_definition(), state_dir=temp_dir())
        rt2.load_state(path)
        after_board = {k: v.status for k, v in rt2._tasks.items()}
        after_inbox = rt2.inbox("worker2")
        assert before_board == after_board
        assert before_inbox == after_inbox
        assert rt2.task(tid).assignee == "worker1"
        assert json.load(open(path))["lifecycle"] == "ACTIVE"

    def test_load_foreign_state_rejected(self):
        rt = TeamRuntime(_definition(name="other"), state_dir=temp_dir())
        path = os.path.join(temp_dir(), "s.json")
        with open(path, "w") as fh:
            json.dump(
                {
                    "definition": {"name": "scan-team"},
                    "lifecycle": "DEFINED",
                    "task_seq": 0,
                    "tasks": {},
                    "inboxes": {},
                },
                fh,
            )
        with pytest.raises(TeamStateError):
            rt.load_state(path)


class TestZeroModelCalls:
    def test_no_model_call_tokens_in_source(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fname in (
            "team.py",
            "team_runtime.py",
            "scoped_permissions.py",
            "dag_node.py",
            "checkpoint_binding.py",
            "trajectory.py",
        ):
            src = open(os.path.join(repo, "fusion_harness", fname)).read()
            for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
                assert banned not in src, "%s references %s" % (fname, banned)


class TestBusEvents:
    def test_task_and_msg_events_flow(self):
        bus = LifecycleBus()
        events = []
        bus.subscribe(lambda ev: events.append(ev.event))
        rt = TeamRuntime(_definition(), bus=bus, state_dir=temp_dir())
        rt.activate()
        rt.add_task(TeamTask(title="t"))
        rt.send(TeamMessage(type="text", sender="lead", recipient="worker1", body="b"))
        assert "team.task.added" in events
        assert "team.msg.sent" in events
