"""Cross-cutting concern ownership: deadline, retry, idempotency, metering.

Issue #13 (spec verification-infra-1108 §3.4): these behaviors were
re-invented per repo — dual timeouts (cow#378), stacked retries
(gw#577 class), hardcoded 120s LLM timeout (as#615), missing
idempotency (cow#381). The kernel owns each concern once; consumers
opt in:

- Deadline: the caller declares a deadline once; callees propagate a
  child() budget and must not add private wrapper timeouts.
- RetryPolicy: single policy type; run_with_retry() mediates execution
  and coalesce_retry() collapses stacked policies — retries never
  stack across layers (a 4-min outage must not become 16 min).
- idempotency_key() / ReceiptCache: (scope, node, action) receipt
  helpers; OutboxStore command receipts (#11) are a ReceiptCache over
  the outbox connection.
- TokenLedger: single authoritative append-only ledger; balance is
  always a recomputed sum — mirror copies rebuild from entries.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ deadline


class DeadlineExceeded(TimeoutError):
    def __init__(self, deadline_at: float, remaining: float, message: str = ""):
        self.deadline_at = deadline_at
        self.remaining = remaining
        super().__init__(message or f"deadline exceeded (remaining={remaining:.3f}s)")


class Deadline:
    # Ownership rule (cow#378): the CALLER declares the deadline once.
    # Callees take a child() budget and must not add private wrapper
    # timeouts of their own.
    def __init__(
        self,
        seconds: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._start = clock()
        self._at = self._start + seconds if seconds is not None else None

    @property
    def at(self) -> float:
        return self._at if self._at is not None else 0.0

    def remaining(self) -> float:
        if self._at is None:
            return float("inf")
        return self._at - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self) -> float:
        rem = self.remaining()
        if rem <= 0.0:
            raise DeadlineExceeded(self.at, rem)
        return rem

    def child(self, seconds: float) -> "Deadline":
        # A callee budget can only SHRINK the parent deadline, never
        # extend it — no layer may outlive the caller's deadline.
        if self._at is None:
            return Deadline(seconds=seconds, clock=self._clock)
        return Deadline(seconds=min(seconds, self.remaining()), clock=self._clock)


# ------------------------------------------------------------------ retry


class TransientError(Exception):
    # Marker base for retryable failures — consumers subclass it for
    # their own transient conditions (5xx, connection resets, ...).
    pass


_DEFAULT_RETRYABLE_TYPES = (TransientError, TimeoutError, ConnectionError, InterruptedError)


def _default_retryable(exc: BaseException) -> bool:
    return isinstance(exc, _DEFAULT_RETRYABLE_TYPES)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_backoff: float = 0.5
    max_backoff: float = 30.0
    multiplier: float = 2.0
    retryable: Callable[[BaseException], bool] = _default_retryable

    def __post_init__(self):
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        if self.base_backoff < 0 or self.max_backoff < 0:
            raise ValueError("backoff values must be >= 0")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")


def coalesce_retry(*policies: RetryPolicy | None) -> RetryPolicy | None:
    # Composition rule (as#615 / gw#577 class): retries never stack
    # across layers. The kernel mediates at the composition site — only
    # the outermost declared policy survives; inner policies are dropped
    # with a loud warning instead of multiplying the outage window.
    picked: RetryPolicy | None = None
    for p in policies:
        if p is None:
            continue
        if picked is None:
            picked = p
        else:
            logger.warning(
                "retry stacking collapsed: inner policy (attempts=%d) dropped, "
                "outermost policy wins",
                p.attempts,
            )
    return picked


def run_with_retry(
    fn: Callable[..., Any],
    policy: RetryPolicy | None = None,
    *args: Any,
    deadline: Deadline | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    # Single retry executor: exponential backoff capped at max_backoff,
    # clamped to the caller's deadline (honest fail, never hang past the
    # deadline — as#649 outage window). Non-retryable errors raise
    # immediately; exhaustion raises the last error.
    policy = policy or RetryPolicy()
    last: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            if deadline is not None:
                deadline.check()
            return fn(*args, **kwargs)
        except Exception as e:
            if not policy.retryable(e) or attempt >= policy.attempts:
                logger.error(
                    "retry stopped attempt=%d/%d retryable=%s error=%s",
                    attempt,
                    policy.attempts,
                    policy.retryable(e),
                    e,
                )
                raise
            last = e
            backoff = min(
                policy.base_backoff * (policy.multiplier ** (attempt - 1)),
                policy.max_backoff,
            )
            if deadline is not None:
                backoff = min(backoff, max(0.0, deadline.remaining()))
            logger.warning(
                "retry attempt=%d/%d backoff=%.2fs error=%s",
                attempt,
                policy.attempts,
                backoff,
                e,
            )
            sleep(backoff)
    raise last  # pragma: no cover - loop always returns or raises


# ------------------------------------------------------------------ idempotency


def idempotency_key(scope_id: str, node_id: str, action: str) -> str:
    # Canonical key shape for every external-write side effect
    # (spec §3.4: scope_id + node_id + action, e.g. run_id + node_id).
    return f"{scope_id}:{node_id}:{action}"


class ReceiptCache:
    # (scope, action) receipt table — standalone consumers without a full
    # outbox register external-write keys here; a replayed key returns the
    # recorded result without re-running the operation (cow#381 duplicate
    # issue/PR prevention lives at this layer).
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        conn: sqlite3.Connection | None = None,
        table: str = "command_receipts",
        clock: Callable[[], float] = time.time,
    ):
        self._owns = conn is None
        self._conn = (
            conn
            if conn is not None
            else sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
        )
        self._conn.row_factory = sqlite3.Row
        self.table = table
        self._clock = clock
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "command_id TEXT PRIMARY KEY, "
            "aggregate TEXT NOT NULL DEFAULT '', "
            "result TEXT, "
            "created_at REAL NOT NULL)"
        )

    def close(self) -> None:
        if self._owns:
            self._conn.close()

    def has(self, command_id: str) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM {self.table} WHERE command_id = ?", (command_id,)
        ).fetchone()
        return row is not None

    def first(
        self,
        command_id: str,
        aggregate: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        row = self._conn.execute(
            f"SELECT result FROM {self.table} WHERE command_id = ?", (command_id,)
        ).fetchone()
        if row is not None:
            logger.info(
                "receipt replayed id=%s aggregate=%s table=%s",
                command_id,
                aggregate,
                self.table,
            )
            return (json.loads(row["result"]) if row["result"] is not None else None), True
        result = fn(*args, **kwargs)
        self._conn.execute(
            f"INSERT INTO {self.table}(command_id, aggregate, result, created_at) "
            "VALUES(?, ?, ?, ?)",
            (command_id, aggregate, json.dumps(result), self._clock()),
        )
        logger.info(
            "receipt recorded id=%s aggregate=%s table=%s",
            command_id,
            aggregate,
            self.table,
        )
        return result, False


# ------------------------------------------------------------------ metering


class TokenLedger:
    # Single authoritative usage ledger (spec §3.4): append-only entries;
    # balance is ALWAYS recomputed as a sum — there is no mutable balance
    # field that can drift from the entry stream. Optional JSONL
    # persistence; mirror copies are rebuildable by replaying entries.
    def __init__(self, path: str | None = None):
        self.path = path
        self._entries: list[dict] = []
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            self._entries.append(json.loads(line))
            except (OSError, ValueError) as e:
                logger.warning("token ledger load failed path=%s err=%s", path, e)

    def append(
        self,
        *,
        scope: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning_tokens: int = 0,
        kind: str = "llm",
        ts: float | None = None,
    ) -> dict:
        # reasoning tokens are recorded but NOT added to total — servers
        # embed reasoning inside completion_tokens (as#483 rule).
        entry = {
            "seq": len(self._entries),
            "ts": ts if ts is not None else time.time(),
            "scope": scope,
            "kind": kind,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "reasoning_tokens": int(reasoning_tokens),
        }
        self._entries.append(entry)
        if self.path:
            try:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError as e:
                logger.warning("token ledger append failed path=%s err=%s", self.path, e)
        logger.info(
            "ledger append scope=%s prompt=%d completion=%d reasoning=%d",
            scope,
            entry["prompt_tokens"],
            entry["completion_tokens"],
            entry["reasoning_tokens"],
        )
        return entry

    def entries(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            return list(self._entries)
        return [e for e in self._entries if e["scope"] == scope]

    def balance(self, scope: str | None = None) -> dict:
        bal = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        for e in self.entries(scope):
            bal["prompt_tokens"] += e["prompt_tokens"]
            bal["completion_tokens"] += e["completion_tokens"]
            bal["reasoning_tokens"] += e["reasoning_tokens"]
        bal["total_tokens"] = bal["prompt_tokens"] + bal["completion_tokens"]
        return bal
