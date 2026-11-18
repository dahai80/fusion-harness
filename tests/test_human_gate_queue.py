"""Tests for the generic HumanGate pending-question queue (issue #66):
create/resolve/list round-trip, requester identity, expiry, single-winner."""

import asyncio

import pytest

from fusion_harness.human_gate import HumanGate, HumanGateError, ReplyValidationError


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _gate(tmp_path, now=1000.0):
    clock = _Clock(now)
    return HumanGate(str(tmp_path), clock=clock), clock


class TestRoundTrip:
    def test_create_wait_resolve(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "proceed?", requester="agent-7")
        assert q.requester == "agent-7"
        assert q.status == "pending"

        async def scenario():
            task = asyncio.ensure_future(gate.wait(q.id, timeout=2.0))
            await asyncio.sleep(0.01)
            out = gate.resolve(q.id, {"ok": True})
            reply = await task
            return reply, out

        reply, out = asyncio.run(scenario())
        assert out["resolved"] is True
        assert reply == {"ok": True}

    def test_resolve_list_round_trip(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q1 = gate.ask("s1", "q1", requester="a")
        q2 = gate.ask("s2", "q2", requester="b")
        listed = gate.list_questions(status="pending")
        assert {x.id for x in listed} == {q1.id, q2.id}
        gate.resolve(q1.id, {"v": 1})
        assert [x.id for x in gate.list_questions(status="pending")] == [q2.id]
        assert [x.id for x in gate.list_questions(status="answered")] == [q1.id]

    def test_requester_filter(self, tmp_path):
        gate, _ = _gate(tmp_path)
        gate.ask("s1", "q", requester="agent-a")
        gate.ask("s1", "q", requester="agent-b")
        assert len(gate.list_questions(requester="agent-a")) == 1

    def test_invalid_reply_rejected_stays_pending(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q", reply_schema={"required": ["ok"]})
        out = gate.resolve(q.id, {"wrong": 1})
        assert out["resolved"] is False
        assert "ok" in out["reason"]
        assert gate.list_questions(status="pending")[0].id == q.id

    def test_invalid_reply_via_reply_raises(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q", reply_schema={"required": ["ok"]})
        with pytest.raises(ReplyValidationError):
            gate.reply(q.id, {})


class TestSingleWinner:
    def test_concurrent_resolve_single_winner(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q")

        async def scenario():
            r1, r2 = await asyncio.gather(
                asyncio.to_thread(gate.resolve, q.id, {"who": 1}),
                asyncio.to_thread(gate.resolve, q.id, {"who": 2}),
            )
            return r1, r2

        r1, r2 = asyncio.run(scenario())
        winners = [r for r in (r1, r2) if r["resolved"]]
        losers = [r for r in (r1, r2) if not r["resolved"]]
        assert len(winners) == 1
        assert len(losers) == 1
        assert losers[0]["reason"] == "answered"

    def test_resolve_after_reply_rejected(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q")
        gate.reply(q.id, {"ok": 1})
        out = gate.resolve(q.id, {"ok": 2})
        assert out == {"resolved": False, "reason": "answered"}


class TestExpiry:
    def test_ttl_expiry_flips_status_on_list(self, tmp_path):
        gate, clock = _gate(tmp_path)
        q = gate.ask("s1", "q", ttl_s=50.0)
        clock.now = 1050.0
        assert gate.list_questions(status="pending") == []
        assert gate.list_questions(status="expired")[0].id == q.id

    def test_expired_rejects_resolve(self, tmp_path):
        gate, clock = _gate(tmp_path)
        q = gate.ask("s1", "q", ttl_s=10.0)
        clock.now = 1010.0
        out = gate.resolve(q.id, {"ok": 1})
        assert out["resolved"] is False
        assert out["reason"] == "expired"

    def test_expired_wait_returns_none(self, tmp_path):
        gate, clock = _gate(tmp_path)
        q = gate.ask("s1", "q", ttl_s=10.0)
        clock.now = 1010.0
        assert asyncio.run(gate.wait(q.id, timeout=0.1)) is None

    def test_reply_on_expired_raises(self, tmp_path):
        gate, clock = _gate(tmp_path)
        q = gate.ask("s1", "q", ttl_s=10.0)
        clock.now = 1010.0
        with pytest.raises(HumanGateError, match="expired"):
            gate.reply(q.id, {"ok": 1})

    def test_no_ttl_never_expires(self, tmp_path):
        gate, clock = _gate(tmp_path)
        gate.ask("s1", "q")
        clock.now = 10_000.0
        assert len(gate.list_questions(status="pending")) == 1

    def test_expiry_survives_restart(self, tmp_path):
        gate, clock = _gate(tmp_path)
        q = gate.ask("s1", "q", ttl_s=10.0)
        clock.now = 1010.0
        gate2 = HumanGate(str(tmp_path), clock=clock)
        assert gate2.list_questions(status="expired")[0].id == q.id


class TestBackwardCompat:
    def test_existing_fields_default(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q")
        assert q.requester == ""
        assert q.expires_at == 0.0

    def test_pending_still_works(self, tmp_path):
        gate, _ = _gate(tmp_path)
        q = gate.ask("s1", "q")
        assert [x.id for x in gate.pending("s1")] == [q.id]
        assert gate.pending("other") == []
