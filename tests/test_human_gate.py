"""Tests for HumanGate (issue #44): queue persistence, reply schema
validation, block/continue policies, session routing, restart survival."""

import asyncio
import json
import os

import pytest

from fusion_harness.human_gate import (
    HumanGate,
    HumanGateError,
    HumanQuestion,
    ReplyValidationError,
    validate_reply,
)
from fusion_harness.funnels import LifecycleBus

SCHEMA = {"required": ["approved"], "properties": {"approved": "bool", "note": "str"}}


def _gate(tmp_path, **kw):
    return HumanGate(str(tmp_path), **kw)


class TestSchemaValidation:
    def test_valid_reply(self):
        out = validate_reply({"approved": True, "note": "ok"}, SCHEMA)
        assert out == {"approved": True, "note": "ok"}

    def test_missing_required(self):
        with pytest.raises(ReplyValidationError, match="required key 'approved'"):
            validate_reply({"note": "x"}, SCHEMA)

    def test_wrong_type(self):
        with pytest.raises(ReplyValidationError, match="expected bool, got str"):
            validate_reply({"approved": "yes"}, SCHEMA)

    def test_bool_never_int_field(self):
        schema = {"properties": {"count": "int"}}
        with pytest.raises(ReplyValidationError, match="expected int, got bool"):
            validate_reply({"count": True}, schema)

    def test_non_dict_reply(self):
        with pytest.raises(ReplyValidationError, match="must be a dict"):
            validate_reply("approve", SCHEMA)

    def test_unknown_type_in_schema(self):
        with pytest.raises(ReplyValidationError, match="unknown reply type"):
            validate_reply({"x": 1}, {"properties": {"x": "tensor"}})

    def test_unspecified_keys_pass(self):
        out = validate_reply({"approved": False, "extra": [1, 2]}, SCHEMA)
        assert out["extra"] == [1, 2]


class TestAskAndPolicies:
    def test_ask_defaults_block_policy(self, tmp_path):
        q = _gate(tmp_path).ask("s1", "Deploy now?")
        assert q.policy == "block"
        assert q.status == "pending"
        assert q.payload == {}
        assert q.id.startswith("hq_")

    def test_bad_policy_rejected(self, tmp_path):
        with pytest.raises(HumanGateError, match="unknown policy"):
            _gate(tmp_path).ask("s1", "q", policy="maybe")

    def test_bad_question_rejected(self, tmp_path):
        with pytest.raises(HumanGateError, match="required"):
            _gate(tmp_path).ask("", "q")

    def test_block_policy_resolves_on_reply(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "Proceed?", reply_schema=SCHEMA)

        async def main():
            task = asyncio.ensure_future(gate.wait(q.id, timeout=5))
            await asyncio.sleep(0.01)
            gate.reply(q.id, {"approved": True})
            return await task

        assert asyncio.run(main()) == {"approved": True}

    def test_block_policy_timeout_returns_none(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "Proceed?")

        async def main():
            return await gate.wait(q.id, timeout=0.05)

        assert asyncio.run(main()) is None
        assert gate.pending("s1")[0].id == q.id

    def test_continue_policy_reply_later_injected(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask(
            "s1", "Pick a name", policy="continue", reply_schema={"properties": {"name": "str"}}
        )
        out = gate.reply(q.id, {"name": "alpha"})
        assert out["session_id"] == "s1"
        assert gate.take_reply(q.id) == {"name": "alpha"}
        assert gate.take_reply(q.id) is None

    def test_reply_twice_rejected(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "q")
        gate.reply(q.id, {})
        with pytest.raises(HumanGateError, match="already answered"):
            gate.reply(q.id, {})

    def test_invalid_reply_stays_pending(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "q", reply_schema=SCHEMA)
        with pytest.raises(ReplyValidationError):
            gate.reply(q.id, {"approved": "nope"})
        assert len(gate.pending("s1")) == 1

    def test_cancel_wakes_waiter(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "q")

        async def main():
            task = asyncio.ensure_future(gate.wait(q.id, timeout=5))
            await asyncio.sleep(0.01)
            gate.cancel(q.id)
            return await task

        assert asyncio.run(main()) is None
        assert gate.pending() == []

    def test_unknown_question_wait_raises(self, tmp_path):
        async def main():
            await _gate(tmp_path).wait("hq_missing")

        with pytest.raises(HumanGateError, match="unknown question"):
            asyncio.run(main())


class TestSessionRouting:
    def test_pending_filtered_by_session(self, tmp_path):
        gate = _gate(tmp_path)
        gate.ask("s1", "q1")
        gate.ask("s2", "q2")
        assert [q.session_id for q in gate.pending("s1")] == ["s1"]
        assert len(gate.pending()) == 2

    def test_reply_routes_to_correct_session(self, tmp_path):
        gate = _gate(tmp_path)
        q1 = gate.ask("s1", "q1")
        q2 = gate.ask("s2", "q2")
        gate.reply(q2.id, {"approved": True})
        assert gate.pending("s1")[0].id == q1.id
        assert gate._questions[q2.id].session_id == "s2"

    def test_wait_after_restart_returns_persisted_reply(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "q")
        gate.reply(q.id, {"approved": False})
        gate2 = _gate(tmp_path)

        async def main():
            return await gate2.wait(q.id)

        assert asyncio.run(main()) == {"approved": False}


class TestPersistence:
    def test_queue_file_written(self, tmp_path):
        _gate(tmp_path).ask("s1", "q", payload={"risk": "high"})
        path = os.path.join(str(tmp_path), "human_gate_queue.json")
        data = json.load(open(path, encoding="utf-8"))
        assert data[0]["payload"] == {"risk": "high"}
        assert data[0]["status"] == "pending"

    def test_pending_survives_restart(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "Survive?", payload={"plan": "p1"}, reply_schema=SCHEMA)
        gate2 = _gate(tmp_path)
        loaded = gate2.pending("s1")
        assert len(loaded) == 1
        assert loaded[0].id == q.id
        assert loaded[0].payload == {"plan": "p1"}
        assert loaded[0].reply_schema == SCHEMA

    def test_answered_reply_survives_restart(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "q")
        gate.reply(q.id, {"approved": True})
        gate2 = _gate(tmp_path)
        assert gate2._questions[q.id].reply == {"approved": True}

    def test_corrupt_entry_skipped_not_fatal(self, tmp_path):
        path = os.path.join(str(tmp_path), "human_gate_queue.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {"id": "hq_ok", "session_id": "s1", "question": "q"},
                    {"id": "", "session_id": "s2", "question": "bad"},
                ],
                fh,
            )
        gate2 = _gate(tmp_path)
        assert len(gate2.pending()) == 1

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = os.path.join(str(tmp_path), "human_gate_queue.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        assert _gate(tmp_path).pending() == []

    def test_atomic_write_no_tmp_left(self, tmp_path):
        _gate(tmp_path).ask("s1", "q")
        assert not os.path.exists(os.path.join(str(tmp_path), "human_gate_queue.json.tmp"))

    def test_question_dataclass_validation(self):
        with pytest.raises(HumanGateError, match="unknown status"):
            HumanQuestion(id="hq_x", session_id="s", question="q", status="open")


class TestLifecycleEvents:
    def test_question_and_reply_emitted(self, tmp_path):
        bus = LifecycleBus()
        seen = []
        bus.subscribe(lambda ev: seen.append(ev.event))
        gate = _gate(tmp_path, bus=bus)
        q = gate.ask("s1", "q")
        gate.reply(q.id, {"approved": True})
        assert "human_gate.question" in seen
        assert "human_gate.reply" in seen

    def test_emit_failure_never_breaks_gate(self, tmp_path):
        class BadBus:
            def emit(self, *a, **kw):
                raise RuntimeError("bus down")

        gate = _gate(tmp_path, bus=BadBus())
        q = gate.ask("s1", "q")
        gate.reply(q.id, {})
        assert gate._questions[q.id].status == "answered"


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "human_gate.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
