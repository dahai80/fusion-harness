"""Tests for checkpoint lineage fork (issue #74): fork into a new lineage
referencing the parent, original stays resumable, full fork chain query,
pruning keeps the lineage root, fork-of-fork works, old stores migrate."""

import sqlite3

import pytest

from fusion_harness.checkpoint import CheckpointError, CheckpointState, CheckpointStore


def _state(tag, msgs=2):
    return CheckpointState(
        messages=[{"role": "user", "content": "%s-%d" % (tag, i)} for i in range(msgs)],
        budget_spent_tokens=len(tag),
        permission_mode="default",
        outbox_offset=7,
        metadata={"tag": tag},
    )


class TestFork:
    def test_fork_creates_new_lineage_with_identical_state(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        store.save("run1", _state("b"))
        fork = store.fork("run1", 2)
        child = store.latest(fork["session_id"])
        assert child.messages == [
            {"role": "user", "content": "b-0"},
            {"role": "user", "content": "b-1"},
        ]
        assert child.budget_spent_tokens == 1
        assert child.outbox_offset == 7
        assert child.lineage_id == fork["session_id"]
        assert child.lineage_id != "run1"
        assert child.parent_checkpoint == {
            "session_id": "run1",
            "seq": 2,
            "lineage_id": "run1",
        }

    def test_original_lineage_untouched_and_resumable(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        store.save("run1", _state("b"))
        fork = store.fork("run1", 1, new_session_id="run1-fork")
        store.save(fork["session_id"], _state("child-progress"))
        assert store.latest("run1").metadata["tag"] == "b"
        assert store.load("run1", 1).metadata["tag"] == "a"
        assert store.latest("run1-fork").metadata["tag"] == "child-progress"
        assert [c["seq"] for c in store.list_checkpoints("run1")] == [1, 2]

    def test_fork_missing_checkpoint_raises(self):
        store = CheckpointStore()
        with pytest.raises(CheckpointError):
            store.fork("ghost", 1)

    def test_explicit_child_session_id(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        fork = store.fork("run1", 1, new_session_id="hotfix")
        assert fork["session_id"] == "hotfix"
        assert store.latest("hotfix") is not None


class TestForkChain:
    def test_root_session_has_empty_chain(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        assert store.fork_chain("run1") == []

    def test_fork_chain_length_one(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        store.save("run1", _state("b"))
        fork = store.fork("run1", 2)
        chain = store.fork_chain(fork["session_id"])
        assert chain == [{"session_id": "run1", "seq": 2, "lineage_id": "run1"}]

    def test_fork_of_fork_off_branch(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        fork1 = store.fork("run1", 1)
        store.save(fork1["session_id"], _state("f1"))
        fork2 = store.fork(fork1["session_id"], 2)
        chain = store.fork_chain(fork2["session_id"])
        assert [c["session_id"] for c in chain] == ["run1", fork1["session_id"]]
        assert chain[-1]["seq"] == 2
        assert store.latest(fork2["session_id"]).lineage_id == fork2["session_id"]

    def test_lineage_query_scoped(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        store.save("run1", _state("b"))
        fork = store.fork("run1", 2)
        store.save(fork["session_id"], _state("c"))
        own = store.lineage_checkpoints("run1")
        assert {r["session_id"] for r in own} == {"run1"}
        child = store.lineage_checkpoints(fork["session_id"])
        assert {r["session_id"] for r in child} == {fork["session_id"]}
        assert [r["seq"] for r in child] == [1, 2]


class TestPrune:
    def test_prune_never_removes_root(self):
        store = CheckpointStore()
        for i in range(5):
            store.save("run1", _state("s%d" % i))
        removed = store.prune("run1", keep_last=2)
        assert removed == 2
        seqs = [c["seq"] for c in store.list_checkpoints("run1")]
        assert seqs == [1, 4, 5]

    def test_prune_noop_under_cap(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        store.save("run1", _state("b"))
        assert store.prune("run1", keep_last=10) == 0

    def test_forked_session_root_is_fork_point(self):
        store = CheckpointStore()
        store.save("run1", _state("a"))
        fork = store.fork("run1", 1)
        for i in range(4):
            store.save(fork["session_id"], _state("p%d" % i))
        store.prune(fork["session_id"], keep_last=1)
        seqs = [c["seq"] for c in store.list_checkpoints(fork["session_id"])]
        assert seqs[0] == 1
        assert store.load(fork["session_id"], 1).parent_checkpoint["session_id"] == "run1"


class TestMigration:
    def test_pre_lineage_store_upgrades(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute(
            "CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL, UNIQUE(session_id, seq))"
        )
        old = CheckpointState(messages=[{"role": "user", "content": "legacy"}])
        conn.execute(
            "INSERT INTO checkpoints (session_id, seq, created_at, state) VALUES ('old', 1, 1.0, ?)",
            (old.to_json(),),
        )
        conn.commit()
        store = CheckpointStore(conn=conn)
        assert store.load("old", 1).messages == [{"role": "user", "content": "legacy"}]
        assert [r["seq"] for r in store.lineage_checkpoints("old")] == [1]
        store.save("old", _state("new"))
        assert len(store.list_checkpoints("old")) == 2
        assert [r["seq"] for r in store.lineage_checkpoints("old")] == [1, 2]

    def test_legacy_state_json_loads_with_defaults(self):
        raw = '{"messages": [], "budget_spent_tokens": 3, "permission_mode": "", "outbox_offset": 0, "metadata": {}}'
        st = CheckpointState.from_json(raw)
        assert st.lineage_id == ""
        assert st.parent_checkpoint == {}
