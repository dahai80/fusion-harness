"""Issue #100: memory consolidation op set — decision policy, tombstones,
bounded retroactive updates, memory_bench four-competency gate."""

from fusion_harness import (
    MemoryConsolidator,
    MemoryOp,
    decide_memory_op,
    is_retrievable,
    memory_similarity,
)
from fusion_harness.memory_consolidation import Neighbor


def nb(key, payload, score=1.0, **meta):
    return Neighbor(key=key, payload=payload, metadata=meta, score=score)


class Sink:
    def __init__(self):
        self.items = {}

    def store(self, key, payload, metadata=None):
        self.items[key] = {"key": key, "payload": payload, "metadata": dict(metadata or {})}

    def retrieve(self, query, limit=5):
        scored = [
            {**i, "score": memory_similarity(query, i["payload"])}
            for i in self.items.values()
            if is_retrievable(i)
        ]
        scored.sort(key=lambda x: -x["score"])
        return scored[:limit]


def test_similarity_jaccard_deterministic():
    assert memory_similarity("deploy on friday", "deploy on friday") == 1.0
    assert memory_similarity("deploy on friday", "cats are cute") == 0.0
    a = memory_similarity("deploy on friday morning", "deploy friday")
    assert 0.0 < a < 1.0


def test_decide_near_duplicate_update():
    d = decide_memory_op("deploy happens on friday", [nb("k1", "deploy on friday", score=0.95)])
    assert d.op == MemoryOp.UPDATE
    assert d.neighbor.key == "k1"


def test_decide_contradiction_supersedes_with_tombstone():
    d = decide_memory_op("release day is monday", [nb("k1", "release day is friday", score=0.4)])
    assert d.op == MemoryOp.UPDATE
    assert "tombstone" in d.reason


def test_decide_retraction_deletes():
    d = decide_memory_op(
        "please retract: deploy on friday", [nb("k1", "deploy on friday", score=0.6)]
    )
    assert d.op == MemoryOp.DELETE


def test_decide_no_neighbor_adds():
    d = decide_memory_op("user likes tea", [nb("k1", "unrelated stuff entirely", score=0.1)])
    assert d.op == MemoryOp.ADD


def test_decide_gray_zone_noop():
    # Overlap in [0.5, 0.92): related but neither duplicate nor unrelated.
    d = decide_memory_op(
        "deploy on friday with rollback and monitoring",
        [nb("k1", "deploy on friday plus runbook", score=0.6)],
    )
    assert d.op == MemoryOp.NOOP


def test_decide_ignores_tombstoned_neighbors():
    d = decide_memory_op(
        "deploy happens on friday",
        [nb("k1", "deploy on friday", score=0.99, tombstoned=True)],
    )
    assert d.op == MemoryOp.ADD


def test_consolidator_add_flow_and_event():
    sink = Sink()

    class Bus:
        events = []

        def emit(self, domain, event, payload, session_id=""):
            Bus.events.append(payload)

    cons = MemoryConsolidator(sink=sink, source=sink, lifecycle_bus=Bus())
    d = cons.consolidate("user prefers tea over coffee", scope="mem")
    assert d.op == MemoryOp.ADD
    assert len(sink.items) == 1
    assert Bus.events and Bus.events[-1]["op"] == "ADD"


def test_consolidator_update_merges_into_neighbor():
    sink = Sink()
    sink.store(key="m1", payload="deploy on friday", metadata={"origin": "user_input"})
    cons = MemoryConsolidator(sink=sink, source=sink)
    d = cons.consolidate("deploy on friday at noon sharp")
    assert d.op == MemoryOp.UPDATE
    assert "at noon sharp" in sink.items["m1"]["payload"]
    assert "[update" in sink.items["m1"]["payload"]


def test_consolidator_delete_tombstones_not_deletes():
    sink = Sink()
    sink.store(key="m1", payload="deploy on friday", metadata={})
    cons = MemoryConsolidator(sink=sink, source=sink)
    d = cons.consolidate("retract the deploy on friday note")
    assert d.op == MemoryOp.DELETE
    assert "m1" in sink.items
    assert sink.items["m1"]["metadata"]["tombstoned"] is True
    assert not is_retrievable(sink.items["m1"])
    # Tombstoned memory no longer retrieved -> new write ADDs.
    d2 = cons.consolidate("deploy on friday again")
    assert d2.op == MemoryOp.ADD


def test_retroactive_updates_bounded():
    sink = Sink()
    for i in range(6):
        sink.store(key="m%d" % i, payload="project alpha milestone %d details" % i, metadata={})
    cons = MemoryConsolidator(sink=sink, source=sink)
    cons.consolidate("project alpha milestone summary refresh")
    assert cons.stats["retroactive"] <= 3


def test_sink_failure_degrades_to_noop_not_crash():
    class BadSink:
        def store(self, *a, **k):
            raise OSError("disk full")

        def retrieve(self, *a, **k):
            return []

    cons = MemoryConsolidator(sink=BadSink(), source=BadSink())
    d = cons.consolidate("some new fact")
    assert d.op == MemoryOp.NOOP
    assert "sink op failed" in d.reason


def test_memory_bridge_optin_consolidation():
    from fusion_harness.memory_link import MemoryBridge

    sink = Sink()
    sink.store(key="m1", payload="deploy on friday", metadata={})
    bridge = MemoryBridge(
        sink=sink, source=sink, consolidator=MemoryConsolidator(sink=sink, source=sink)
    )
    ok = bridge.persist_compaction("deploy on friday at noon sharp", session_id="s9")
    assert ok
    assert "at noon sharp" in sink.items["m1"]["payload"]


def test_memory_bridge_default_path_unchanged():
    from fusion_harness.memory_link import MemoryBridge

    sink = Sink()
    bridge = MemoryBridge(sink=sink, source=sink)
    ok = bridge.persist_compaction("plain summary text", session_id="s1")
    assert ok
    assert any("plain summary text" in i["payload"] for i in sink.items.values())
    assert bridge.consolidator is None
