"""Issue #13: cross-cutting concern ownership (spec §3.4).

- Deadline: caller declares once, children only shrink, honest expiry
- RetryPolicy + run_with_retry: single mediation point, deadline-clamped
  backoff, non-retryable raises immediately, stacking collapsed
- ReceiptCache: replayed key = no-op with recorded result
- TokenLedger: append-only, balance recomputed as sum, JSONL rebuild
- OutboxStore delegates command receipts to ReceiptCache (#11 x #13)

Runner: pytest tests/test_policies.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fusion_harness import (
    Deadline,
    DeadlineExceeded,
    OutboxStore,
    ReceiptCache,
    RetryPolicy,
    TokenLedger,
    TransientError,
    coalesce_retry,
    idempotency_key,
    run_with_retry,
)
from fusion_harness.policies import _default_retryable


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class TestDeadline:
    def test_remaining_and_expired(self):
        clock = FakeClock(100.0)
        d = Deadline(10.0, clock=clock)
        assert d.remaining() == pytest.approx(10.0)
        assert not d.expired()
        clock.advance(9.5)
        assert d.remaining() == pytest.approx(0.5)
        clock.advance(0.5)
        assert d.expired()

    def test_check_raises_when_expired(self):
        clock = FakeClock()
        d = Deadline(1.0, clock=clock)
        clock.advance(1.0)
        with pytest.raises(DeadlineExceeded):
            d.check()
        assert isinstance(d.check.__self__, Deadline)

    def test_deadline_exceeded_is_timeout_error(self):
        assert issubclass(DeadlineExceeded, TimeoutError)

    def test_unbounded_never_expires(self):
        d = Deadline(clock=FakeClock())
        assert d.remaining() == float("inf")
        assert not d.expired()
        d.check()

    def test_child_only_shrinks_parent(self):
        clock = FakeClock()
        parent = Deadline(10.0, clock=clock)
        child = parent.child(4.0)
        assert child.remaining() <= 4.0
        # callee may not extend the caller's deadline
        greedy = parent.child(100.0)
        assert greedy.remaining() <= parent.remaining()

    def test_child_of_expired_parent_is_expired(self):
        clock = FakeClock()
        parent = Deadline(1.0, clock=clock)
        clock.advance(2.0)
        assert parent.child(5.0).expired()


class TestRetryPolicy:
    def test_validation(self):
        with pytest.raises(ValueError):
            RetryPolicy(attempts=0)
        with pytest.raises(ValueError):
            RetryPolicy(base_backoff=-1)
        with pytest.raises(ValueError):
            RetryPolicy(multiplier=0.5)

    def test_default_retryable_classifier(self):
        assert _default_retryable(TransientError())
        assert _default_retryable(TimeoutError())
        assert _default_retryable(ConnectionError())
        assert not _default_retryable(ValueError())


class TestRunWithRetry:
    def test_success_first_attempt_no_sleep(self):
        calls = []
        sleeps = []
        result = run_with_retry(
            lambda: calls.append(1) or "ok",
            RetryPolicy(attempts=3),
            sleep=sleeps.append,
        )
        assert result == "ok"
        assert len(calls) == 1 and sleeps == []

    def test_transient_then_success(self):
        attempts = {"n": 0}
        sleeps = []

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientError("503")
            return "done"

        result = run_with_retry(
            flaky, RetryPolicy(attempts=5, base_backoff=0.1), sleep=sleeps.append
        )
        assert result == "done"
        assert attempts["n"] == 3
        assert len(sleeps) == 2

    def test_exhaustion_raises_last_error(self):
        def always_down():
            raise TransientError("down")

        with pytest.raises(TransientError):
            run_with_retry(
                always_down, RetryPolicy(attempts=3, base_backoff=0.0), sleep=lambda s: None
            )

    def test_non_retryable_raises_immediately(self):
        attempts = {"n": 0}
        sleeps = []

        def bad_input():
            attempts["n"] += 1
            raise ValueError("content failure")

        with pytest.raises(ValueError):
            run_with_retry(bad_input, RetryPolicy(attempts=5), sleep=sleeps.append)
        assert attempts["n"] == 1 and sleeps == []

    def test_backoff_capped_at_max(self):
        sleeps = []

        def down():
            raise TransientError("down")

        with pytest.raises(TransientError):
            run_with_retry(
                down,
                RetryPolicy(attempts=5, base_backoff=1.0, max_backoff=2.0),
                sleep=sleeps.append,
            )
        assert max(sleeps) <= 2.0

    def test_deadline_clamps_backoff_and_fails_honestly(self):
        clock = FakeClock()
        sleeps = []

        def down():
            raise TransientError("down")

        d = Deadline(1.0, clock=clock)

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock.advance(s)

        with pytest.raises(DeadlineExceeded):
            run_with_retry(
                down, RetryPolicy(attempts=10, base_backoff=0.6), deadline=d, sleep=fake_sleep
            )
        # total wall time never ran past the caller's deadline window
        assert clock.now <= 1.0 + 1e-9


class TestCoalesceRetry:
    def test_outermost_wins_and_inner_dropped(self):
        outer = RetryPolicy(attempts=5)
        inner = RetryPolicy(attempts=3)
        assert coalesce_retry(outer, inner) is outer
        assert coalesce_retry(None, inner) is inner
        assert coalesce_retry(None, None) is None


class TestIdempotencyKey:
    def test_shape(self):
        assert idempotency_key("run-1", "node-a", "github_write") == "run-1:node-a:github_write"


class TestReceiptCache:
    def test_first_runs_once_replay_returns_recorded(self):
        cache = ReceiptCache()
        calls = {"n": 0}

        def work():
            calls["n"] += 1
            return {"issue": 42}

        result, replayed = cache.first("scope-a:create_issue", "github", work)
        assert result == {"issue": 42} and replayed is False and calls["n"] == 1
        again, replayed2 = cache.first("scope-a:create_issue", "github", work)
        assert again == {"issue": 42} and replayed2 is True and calls["n"] == 1
        assert cache.has("scope-a:create_issue")
        assert not cache.has("other")

    def test_none_result_cached(self):
        cache = ReceiptCache()
        calls = {"n": 0}

        def work():
            calls["n"] += 1
            return None

        cache.first("k", "agg", work)
        cache.first("k", "agg", work)
        assert calls["n"] == 1

    def test_persistence_across_instances(self, tmp_path: Path):
        db = str(tmp_path / "receipts.db")
        c1 = ReceiptCache(db)
        c1.first("k1", "agg", lambda: "v1")
        c1.close()
        c2 = ReceiptCache(db)
        val, replayed = c2.first("k1", "agg", lambda: "v2")
        assert val == "v1" and replayed is True
        c2.close()


class TestTokenLedger:
    def test_balance_is_recomputed_sum(self, tmp_path: Path):
        ledger = TokenLedger()
        ledger.append(scope="run-1", prompt_tokens=10, completion_tokens=20)
        b1 = ledger.balance("run-1")
        ledger.append(scope="run-1", prompt_tokens=5, completion_tokens=5)
        b2 = ledger.balance("run-1")
        # balance moved with the entry stream — nothing mutable stored
        assert b1["total_tokens"] == 30
        assert b2["total_tokens"] == 40
        assert b2["prompt_tokens"] == 15 and b2["completion_tokens"] == 25

    def test_scope_filter(self):
        ledger = TokenLedger()
        ledger.append(scope="a", prompt_tokens=1, completion_tokens=1)
        ledger.append(scope="b", prompt_tokens=3, completion_tokens=3)
        assert ledger.balance("a")["total_tokens"] == 2
        assert ledger.balance("b")["total_tokens"] == 6
        assert ledger.balance()["total_tokens"] == 8
        assert len(ledger.entries()) == 2 and len(ledger.entries("a")) == 1

    def test_reasoning_not_double_counted_in_total(self):
        ledger = TokenLedger()
        ledger.append(scope="a", prompt_tokens=10, completion_tokens=20, reasoning_tokens=8)
        b = ledger.balance("a")
        assert b["reasoning_tokens"] == 8
        assert b["total_tokens"] == 30

    def test_jsonl_persistence_rebuilds_balance(self, tmp_path: Path):
        path = str(tmp_path / "ledger.jsonl")
        l1 = TokenLedger(path)
        l1.append(scope="run-1", prompt_tokens=10, completion_tokens=5)
        l1.append(scope="run-2", prompt_tokens=7, completion_tokens=3)
        raw = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
        assert len(raw) == 2
        l2 = TokenLedger(path)
        assert l2.balance()["total_tokens"] == 25
        assert l2.balance("run-2")["total_tokens"] == 10


class TestOutboxDelegatesReceipts:
    def test_outbox_receipts_via_shared_cache(self, tmp_path: Path):
        store = OutboxStore(str(tmp_path / "outbox.db"))
        calls = {"n": 0}

        def work():
            calls["n"] += 1
            return {"pr": 7}

        result, replayed = store.record_command("cmd-1", "github", work)
        assert result == {"pr": 7} and replayed is False and calls["n"] == 1
        result2, replayed2 = store.record_command("cmd-1", "github", work)
        assert result2 == {"pr": 7} and replayed2 is True and calls["n"] == 1
        assert store.has_receipt("cmd-1")
        store.close()

    def test_outbox_effect_receipt_key_uses_kernel_helper(self, tmp_path: Path):
        store = OutboxStore(str(tmp_path / "outbox.db"))
        ids = store.commit_step(
            "run-9",
            [{"type": "step", "payload": {}}],
            [{"node_id": "n1", "action": "pr_write", "payload": {}}],
        )
        effect = store.effect(ids[0])
        assert effect.receipt_key == "run-9:n1:pr_write"
        # same (run, node, action) dedupes
        ids2 = store.commit_step(
            "run-9",
            [],
            [{"node_id": "n1", "action": "pr_write", "payload": {}}],
        )
        assert ids2 == []
        store.close()
