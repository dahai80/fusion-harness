"""Issue #125: durable awaiting_human checkpoints for HumanGate pauses."""

from fusion_harness import (
    AWAITING_HUMAN_MARKER,
    CheckpointStore,
    HumanGate,
)


def _gate(tmp_path, **kw):
    return HumanGate(str(tmp_path / "gate"), **kw)


class TestAwaitingHumanCheckpoint:
    def test_unwired_gate_unchanged(self, tmp_path):
        gate = _gate(tmp_path)
        q = gate.ask("s1", "proceed?", policy="continue")
        assert q.status == "pending"
        assert "checkpoint_seq" not in q.payload

    def test_ask_writes_awaiting_human_checkpoint(self, tmp_path):
        store = CheckpointStore(str(tmp_path / "cp.db"))
        gate = _gate(tmp_path, checkpoint_ref=store)
        q = gate.ask("s1", "delete 200 rows?", policy="block", payload={"rows": 200})
        seq = q.payload["checkpoint_seq"]
        state = store.load("s1", seq)
        assert state is not None
        assert state.permission_mode == AWAITING_HUMAN_MARKER
        meta = state.metadata["awaiting_human"]
        assert meta["question_id"] == q.id
        assert meta["checkpoint_kind"] == "pending"
        assert meta["payload"] == {"rows": 200}
        assert meta["question"] == "delete 200 rows?"

    def test_reply_updates_checkpoint_in_place(self, tmp_path):
        store = CheckpointStore(str(tmp_path / "cp.db"))
        gate = _gate(tmp_path, checkpoint_ref=store)
        q = gate.ask(
            "s1", "proceed?", reply_schema={"required": ["ok"], "properties": {"ok": "bool"}}
        )
        seq = q.payload["checkpoint_seq"]
        gate.reply(q.id, {"ok": True})
        state = store.load("s1", seq)
        meta = state.metadata["awaiting_human"]
        assert meta["checkpoint_kind"] == "answered"
        assert meta["reply"] == {"ok": True}
        # in place: no checkpoint sprawl
        assert len(store.list_checkpoints("s1")) == 1

    def test_cancel_updates_checkpoint(self, tmp_path):
        store = CheckpointStore(str(tmp_path / "cp.db"))
        gate = _gate(tmp_path, checkpoint_ref=store)
        q = gate.ask("s1", "proceed?")
        seq = q.payload["checkpoint_seq"]
        gate.cancel(q.id)
        meta = store.load("s1", seq).metadata["awaiting_human"]
        assert meta["checkpoint_kind"] == "cancelled"

    def test_resolve_updates_checkpoint(self, tmp_path):
        store = CheckpointStore(str(tmp_path / "cp.db"))
        gate = _gate(tmp_path, checkpoint_ref=store)
        q = gate.ask("s1", "proceed?")
        seq = q.payload["checkpoint_seq"]
        gate.resolve(q.id, {"ok": True})
        meta = store.load("s1", seq).metadata["awaiting_human"]
        assert meta["checkpoint_kind"] == "answered"

    def test_checkpoint_failure_never_blocks_question(self, tmp_path):
        class _Broken:
            def save(self, *a, **kw):
                raise RuntimeError("db locked")

        gate = _gate(tmp_path, checkpoint_ref=_Broken())
        q = gate.ask("s1", "proceed?")
        assert q.status == "pending"
        assert gate.get_question(q.id) is q

    def test_restart_redelivers_from_queue_and_checkpoint(self, tmp_path):
        store = CheckpointStore(str(tmp_path / "cp.db"))
        gate = _gate(tmp_path, checkpoint_ref=store)
        q = gate.ask("s1", "proceed?")
        seq = q.payload["checkpoint_seq"]
        # simulate restart: fresh gate instance over the same state dirs
        gate2 = HumanGate(str(tmp_path / "gate"), checkpoint_ref=store)
        pending = gate2.pending("s1")
        assert [p.id for p in pending] == [q.id]
        state = store.load("s1", seq)
        assert state.metadata["awaiting_human"]["question_id"] == q.id
