"""Issue #11: transactional outbox + idempotent command receipts + reconcile.

t3code semantics mapped to the harness kernel:
- commit_step: run events + side-effect enqueue commit in ONE sqlite
  transaction — kill -9 after commit loses nothing, before commit leaves
  no partial state
- record_command: replayed command_id is a no-op returning the recorded
  result (duplicate GitHub-issue/PR prevention, cowork#381)
- settle(failed, backoff): persisted available_at, survives restart
- reconcile: expired leases requeue, orphaned-run effects retire

Runner: pytest tests/test_outbox.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fusion_harness import OutboxStore, ReceiptReplayed, ReconcileReport


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def store(tmp_path: Path, clock):
    s = OutboxStore(str(tmp_path / "outbox.db"), clock=clock)
    yield s
    s.close()


class TestTransactionalCommit:
    def test_events_and_effects_commit_atomically(self, store):
        ids = store.commit_step(
            "run1",
            [{"type": "node_done", "payload": {"node": "n1"}}],
            [{"node_id": "n1", "action": "github_create_issue", "payload": {"title": "t"}}],
        )
        assert len(ids) == 1
        assert store.events_for_run("run1")[0]["type"] == "node_done"
        fx = store.effect(ids[0])
        assert fx.status == "pending"
        assert fx.action == "github_create_issue"

    def test_commit_failure_leaves_no_partial_state(self, store, monkeypatch):
        # A malformed effect payload fails json.dumps inside the transaction;
        # neither the events nor earlier effects may survive.
        store.commit_step(
            "run1",
            [{"type": "e1", "payload": {}}],
            [{"node_id": "n1", "action": "a1", "payload": {}}],
        )
        before_events = len(store.events_for_run("run1"))
        with pytest.raises(TypeError):
            store.commit_step(
                "run2",
                [
                    {"type": "ok", "payload": {}},
                    {"type": "bad", "payload": {"x": object()}},
                ],
                [],
            )
        assert len(store.events_for_run("run1")) == before_events
        assert store.events_for_run("run2") == []

    def test_receipt_key_dedupes_effect(self, store):
        fx = {"node_id": "n1", "action": "a", "payload": {}}
        first = store.commit_step("run1", [], [fx])
        second = store.commit_step("run1", [], [fx])
        assert len(first) == 1 and second == []


class TestKill9Survival:
    def test_state_survives_reopen_midrun(self, tmp_path, clock):
        db = str(tmp_path / "kill.db")
        s1 = OutboxStore(db, clock=clock)
        ids = s1.commit_step(
            "run1",
            [{"type": "node_done", "payload": {"node": "n6"}}],
            [{"node_id": "n6", "action": "github_create_issue", "payload": {"issue": 19}}],
        )
        fx = s1.claim_next()
        assert fx.id == ids[0]
        # kill -9: no settle, process gone
        s1._conn.close()
        s2 = OutboxStore(db, clock=clock)
        # effect is in_flight with a lease; it is NOT re-claimed early...
        assert s2.claim_next() is None
        # ...until the lease expires and reconcile() requeues it
        clock.advance(61)
        report = s2.reconcile()
        assert report.requeued_effects == 1
        fx2 = s2.claim_next()
        assert fx2.id == ids[0]
        assert fx2.attempts == 2
        assert fx2.action == "github_create_issue"
        s2.close()

    def test_events_persisted_before_any_effect_execution(self, tmp_path, clock):
        db = str(tmp_path / "evt.db")
        s1 = OutboxStore(db, clock=clock)
        s1.commit_step("run1", [{"type": "step", "payload": {"i": 1}}], [])
        s1._conn.close()
        s2 = OutboxStore(db, clock=clock)
        assert s2.events_for_run("run1")[0]["payload"] == {"i": 1}
        s2.close()


class TestCommandReceipts:
    def test_replayed_command_is_noop(self, store, clock):
        calls = []

        def create_issue(title):
            calls.append(title)
            return {"issue": 19}

        r1, replayed1 = store.record_command("cmd-1", "delivery", create_issue, "x")
        r2, replayed2 = store.record_command("cmd-1", "delivery", create_issue, "x")
        assert r1 == r2 == {"issue": 19}
        assert (replayed1, replayed2) == (False, True)
        assert calls == ["x"], "fn must run exactly once"

    def test_execute_command_raises_on_replay(self, store):
        store.record_command("cmd-1", "agg", lambda: 7)
        with pytest.raises(ReceiptReplayed) as exc:
            store.execute_command("cmd-1", "agg", lambda: 7)
        assert exc.value.result == 7
        assert store.has_receipt("cmd-1")

    def test_none_result_is_cached_too(self, store):
        r1, replayed1 = store.record_command("cmd-n", "agg", lambda: None)
        r2, replayed2 = store.record_command("cmd-n", "agg", lambda: None)
        assert r1 is None and r2 is None
        assert replayed2 is True


class TestPersistedBackoff:
    def test_failed_effect_waits_out_backoff(self, store, clock):
        (eid,) = store.commit_step("run1", [], [{"node_id": "n1", "action": "a", "payload": {}}])
        store.claim_next()
        store.settle(eid, ok=False, error="rate limited", backoff_seconds=30.0)
        fx = store.effect(eid)
        assert fx.status == "pending"
        clock.advance(10)
        assert store.claim_next() is None, "backoff must gate re-claim"
        clock.advance(20)
        fx2 = store.claim_next()
        assert fx2.id == eid

    def test_backoff_survives_reopen(self, tmp_path, clock):
        db = str(tmp_path / "backoff.db")
        s1 = OutboxStore(db, clock=clock)
        (eid,) = s1.commit_step("run1", [], [{"node_id": "n", "action": "a", "payload": {}}])
        s1.claim_next()
        s1.settle(eid, ok=False, error="boom", backoff_seconds=120.0)
        s1._conn.close()
        s2 = OutboxStore(db, clock=clock)
        clock.advance(60)
        assert s2.claim_next() is None, "persisted available_at must survive restart"
        clock.advance(60)
        assert s2.claim_next().id == eid
        s2.close()


class TestReconcile:
    def test_orphaned_run_effects_retire(self, store):
        ids = store.commit_step("lost-run", [], [{"node_id": "n", "action": "a", "payload": {}}])
        store.commit_step("live-run", [], [{"node_id": "n", "action": "a", "payload": {}}])
        report = store.reconcile(orphaned_run_ids=["lost-run"])
        assert isinstance(report, ReconcileReport)
        assert report.retired_effects == 1
        assert store.effect(ids[0]).status == "retired"
        assert store.pending_count() == 1

    def test_expired_lease_requeues_before_retire_scan(self, tmp_path, clock):
        db = str(tmp_path / "rec.db")
        s = OutboxStore(db, clock=clock, lease_seconds=10.0)
        (eid,) = s.commit_step("run1", [], [{"node_id": "n", "action": "a", "payload": {}}])
        s.claim_next()
        clock.advance(11)
        report = s.reconcile(orphaned_run_ids=[])  # run not orphaned
        assert report.requeued_effects == 1
        assert s.effect(eid).status == "pending"
        s.close()


class TestSerialClaim:
    def test_only_one_in_flight_at_a_time(self, store):
        store.commit_step(
            "run1",
            [],
            [
                {"node_id": "n1", "action": "a1", "payload": {}},
                {"node_id": "n2", "action": "a2", "payload": {}},
            ],
        )
        fx1 = store.claim_next()
        fx2 = store.claim_next()
        assert fx1.action != fx2.action
        assert store.claim_next() is None, "all claimed, nothing pending"
        store.settle(fx1.id, ok=True)
        store.settle(fx2.id, ok=True)
        assert store.pending_count() == 0

    def test_fifo_delivery_order(self, store):
        store.commit_step(
            "run1",
            [],
            [
                {"node_id": "n1", "action": "first", "payload": {}},
                {"node_id": "n1", "action": "second", "payload": {}},
            ],
        )
        assert store.claim_next().action == "first"
        store.settle(store.effect_by_receipt("run1:n1:first").id, ok=True)
        assert store.claim_next().action == "second"
