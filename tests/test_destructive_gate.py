"""Tests for the host-owned destructive-call approval gate (issue #73):
table-driven classification, server-side confirm never authorizes, #66
queue integration, idempotent single-redemption tokens, replay rejection,
telemetry for granted / denied / timeout."""

import asyncio
from types import SimpleNamespace

from fusion_harness.destructive_gate import (
    OUTCOME_DENIED,
    OUTCOME_GRANTED,
    OUTCOME_NOT_DESTRUCTIVE,
    OUTCOME_REPLAY,
    OUTCOME_TIMEOUT,
    ApprovalGate,
)
from fusion_harness.funnels import LifecycleBus, ToolFunnel
from fusion_harness.human_gate import HumanGate
from fusion_harness.permissions import PermissionDecision, PermissionEngine


def _gate(tmp_path, bus=None):
    hg = HumanGate(str(tmp_path))
    return ApprovalGate(hg, bus=bus, ttl_s=60.0), hg


class TestClassification:
    def test_policy_table_exact(self):
        assert ApprovalGate.classify("git.force_push") == (
            True,
            "irreversible history rewrite",
        )
        assert ApprovalGate.classify("payment.charge")[0] is True

    def test_name_token_heuristic(self):
        assert ApprovalGate.classify("db_delete_rows")[0] is True
        assert ApprovalGate.classify("s3_object_remove")[0] is True

    def test_safe_tools(self):
        assert ApprovalGate.classify("fs.read") == (False, "")
        assert ApprovalGate.classify("web.search") == (False, "")

    def test_server_confirmation_never_classifies_or_authorizes(self):
        # A server-side confirm field must not flip classification nor count
        # as approval — the seam attack from arXiv 2611.02527.
        destructive, _ = ApprovalGate.classify("fs.read", {"confirm": True})
        assert destructive is False
        destructive2, _ = ApprovalGate.classify("shell.rm", {"server_confirmed": True})
        assert destructive2 is True


class TestTokenLifecycle:
    def test_issue_idempotent(self, tmp_path):
        gate, _ = _gate(tmp_path)
        t1 = gate.issue_token("c1")
        t2 = gate.issue_token("c1")
        assert t1 == t2
        assert gate.issue_token("c2") != t1

    def test_replay_across_call_ids_rejected(self, tmp_path):
        gate, _ = _gate(tmp_path)
        token = gate.issue_token("c1")
        assert gate.redeem(token, "c1")["redeemed"] is True
        assert gate.redeem(token, "c2") == {"redeemed": False, "reason": OUTCOME_REPLAY}

    def test_double_redemption_rejected(self, tmp_path):
        gate, _ = _gate(tmp_path)
        token = gate.issue_token("c1")
        assert gate.redeem(token, "c1")["redeemed"] is True
        assert gate.redeem(token, "c1")["redeemed"] is False

    def test_unknown_token_rejected(self, tmp_path):
        gate, _ = _gate(tmp_path)
        assert gate.redeem("apt_ghost", "c1")["redeemed"] is False


class TestApprovalFlow:
    def test_non_destructive_passthrough(self, tmp_path):
        gate, _ = _gate(tmp_path)
        decision = gate.request("c1", "fs.read", "s1")
        assert decision.outcome == OUTCOME_NOT_DESTRUCTIVE
        assert decision.destructive is False

    def test_request_creates_host_question(self, tmp_path):
        gate, hg = _gate(tmp_path)
        decision = gate.request("c1", "shell.rm", "s1", {"path": "/tmp/x"})
        assert decision.outcome == "pending"
        q = hg.get_question(decision.question_id)
        assert q.requester == "host"
        assert q.payload["call_id"] == "c1"

    def test_grant_proceeds_exactly_once(self, tmp_path):
        gate, hg = _gate(tmp_path)
        decision = gate.request("c1", "shell.rm", "s1")

        async def scenario():
            hg.resolve(decision.question_id, {"approved": True})
            return await gate.decide("c1", timeout=2.0)

        decision = asyncio.run(scenario())
        assert decision.outcome == OUTCOME_GRANTED
        assert decision.token
        assert gate.redeem(decision.token, "c1")["redeemed"] is True
        assert gate.redeem(decision.token, "c1")["redeemed"] is False

    def test_deny_stops(self, tmp_path):
        gate, hg = _gate(tmp_path)
        decision = gate.request("c1", "shell.rm", "s1")

        async def scenario():
            hg.resolve(decision.question_id, {"approved": False})
            return await gate.decide("c1", timeout=2.0)

        decision = asyncio.run(scenario())
        assert decision.outcome == OUTCOME_DENIED
        assert not gate.has_grant("c1")

    def test_timeout(self, tmp_path):
        gate, hg = _gate(tmp_path)
        gate.request("c1", "shell.rm", "s1")

        async def scenario():
            return await gate.decide("c1", timeout=0.05)

        decision = asyncio.run(scenario())
        assert decision.outcome == OUTCOME_TIMEOUT
        assert hg.get_question(decision.question_id).status == "cancelled"

    def test_decide_cached_after_final(self, tmp_path):
        gate, hg = _gate(tmp_path)
        decision = gate.request("c1", "shell.rm", "s1")

        async def scenario():
            hg.resolve(decision.question_id, {"approved": True})
            first = await gate.decide("c1")
            second = await gate.decide("c1")
            return first, second

        first, second = asyncio.run(scenario())
        assert first.token == second.token
        assert first.outcome == second.outcome == OUTCOME_GRANTED

    def test_permission_engine_deny_short_circuits(self, tmp_path):
        gate, hg = _gate(tmp_path)
        gated = ApprovalGate(
            hg,
            permission_engine=type(
                "E",
                (),
                {
                    "evaluate": staticmethod(
                        lambda *a, **k: PermissionDecision(decision="deny", reason="no")
                    )
                },
            )(),
        )
        decision = gated.request("c1", "shell.rm", "s1")
        assert decision.outcome == OUTCOME_DENIED
        assert gate._questions == {}  # noqa: SLF001 - no question ever opened
        assert hg.list_questions(status="pending") == []


class TestTelemetry:
    def _bus_events(self, bus):
        return [(e.domain, e.event) for e in bus.replay()]

    def test_all_three_outcomes_emit(self, tmp_path):
        bus = LifecycleBus()
        gate, hg = _gate(tmp_path, bus=bus)
        d_grant = gate.request("c1", "shell.rm", "s1")
        d_deny = gate.request("c2", "payment.charge", "s1")

        async def scenario():
            hg.resolve(d_grant.question_id, {"approved": True})
            hg.resolve(d_deny.question_id, {"approved": False})
            await gate.decide("c1")
            await gate.decide("c2")
            gate.request("c3", "git.force_push", "s1")
            return await gate.decide("c3", timeout=0.05)

        asyncio.run(scenario())
        events = self._bus_events(bus)
        names = [e[1] for e in events]
        assert "destructive_granted" in names
        assert "destructive_denied" in names
        assert "destructive_timeout" in names

    def test_replay_emits_telemetry(self, tmp_path):
        bus = LifecycleBus()
        gate, _ = _gate(tmp_path, bus=bus)
        token = gate.issue_token("c1")
        gate.redeem(token, "c1")
        gate.redeem(token, "c2")
        assert "destructive_token_replay" in [e.event for e in bus.replay()]


class TestFunnelWiring:
    def _funnel(self, gate):
        return ToolFunnel(permissions=PermissionEngine(mode="bypass"), approval_gate=gate)

    def _ctx(self):
        return SimpleNamespace(session_id="s1", graph_id="")

    def test_destructive_blocked_without_grant(self, tmp_path):
        gate, _ = _gate(tmp_path)
        funnel = self._funnel(gate)
        outcome = asyncio.run(funnel.admit(self._ctx(), tool_name="shell.rm", args={"path": "/x"}))
        assert outcome.allowed is False
        assert "host approval" in outcome.reason

    def test_server_confirmation_alone_does_not_execute(self, tmp_path):
        gate, _ = _gate(tmp_path)
        funnel = self._funnel(gate)
        outcome = asyncio.run(
            funnel.admit(
                self._ctx(),
                tool_name="shell.rm",
                args={"path": "/x", "server_confirmed": True, "confirm": True},
            )
        )
        assert outcome.allowed is False

    def test_destructive_executes_with_grant(self, tmp_path):
        gate, hg = _gate(tmp_path)
        funnel = self._funnel(gate)
        decision = gate.request("c1", "shell.rm", "s1", {"path": "/x"})

        async def scenario():
            hg.resolve(decision.question_id, {"approved": True})
            await gate.decide("c1")
            return gate.redeem(gate.get_decision("c1").token, "c1")

        assert asyncio.run(scenario())["redeemed"] is True
        outcome = asyncio.run(
            funnel.admit(self._ctx(), tool_name="shell.rm", args={"path": "/x", "call_id": "c1"})
        )
        assert outcome.allowed is True

    def test_non_destructive_unaffected(self, tmp_path):
        gate, _ = _gate(tmp_path)
        funnel = self._funnel(gate)
        outcome = asyncio.run(funnel.admit(self._ctx(), tool_name="fs.read", args={}))
        assert outcome.allowed is True
