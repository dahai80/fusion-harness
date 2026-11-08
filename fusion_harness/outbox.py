"""Transactional outbox + idempotent command receipts + startup reconcile.

Issue #11: t3code reliability core, Python-native, stdlib-only (sqlite3).
Semantics mapping:
- t3code EventSink one-transaction persist  -> OutboxStore.commit_step()
  (run events + side-effect enqueue commit in ONE sqlite transaction)
- t3code EffectWorker claim/execute/settle  -> OutboxStore.claim_next() /
  settle() with persisted available_at backoff (at-least-once delivery)
- t3code idempotent command receipts        -> OutboxStore.record_command()
  (a replayed command_id is a no-op returning the recorded result)
- t3code ProviderRuntimeRecoveryService     -> OutboxStore.reconcile()
  (expired leases requeue, orphaned-run effects retire)

Crash model: WAL + synchronous=FULL — a kill -9 between commit_step and
effect execution loses nothing; the effect is re-delivered on restart and
consumers dedupe via receipts / (run_id, node_id, action) receipt keys.
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

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_receipts (
    command_id TEXT PRIMARY KEY,
    aggregate TEXT NOT NULL DEFAULT '',
    result TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_key TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    available_at REAL NOT NULL,
    lease_expires REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_receipt
    ON outbox(receipt_key);
CREATE INDEX IF NOT EXISTS idx_outbox_claim
    ON outbox(status, available_at);
CREATE INDEX IF NOT EXISTS idx_outbox_run
    ON outbox(run_id, status);
CREATE INDEX IF NOT EXISTS idx_run_events_run
    ON run_events(run_id, id);
"""

PENDING = "pending"
IN_FLIGHT = "in_flight"
DONE = "done"
RETIRED = "retired"


class ReceiptReplayed(Exception):
    """Raised by execute_command when command_id already has a receipt."""

    def __init__(self, command_id: str, result: Any):
        self.command_id = command_id
        self.result = result
        super().__init__(f"command already executed: {command_id}")


@dataclass
class Effect:
    id: int
    receipt_key: str
    run_id: str
    node_id: str
    action: str
    payload: dict
    status: str
    attempts: int
    available_at: float
    last_error: str | None = None


@dataclass
class ReconcileReport:
    requeued_effects: int
    retired_effects: int
    orphaned_runs: int

    def __str__(self) -> str:
        return (
            f"reconcile: requeued={self.requeued_effects} "
            f"retired={self.retired_effects} orphaned_runs={self.orphaned_runs}"
        )


class OutboxStore:
    def __init__(
        self,
        db_path: str,
        *,
        lease_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        self.db_path = db_path
        self.lease_seconds = lease_seconds
        self._clock = clock
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO NOTHING",
            (str(_SCHEMA_VERSION),),
        )
        logger.info("outbox store opened db=%s lease=%.1fs", db_path, lease_seconds)

    def close(self) -> None:
        self._conn.close()

    # -------------------------------------------------- command receipts

    def record_command(
        self,
        command_id: str,
        aggregate: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        # Idempotent command execution: a replayed command_id returns the
        # recorded result without re-running fn (cow#381 duplicate-issue
        # prevention lives at this layer).
        row = self._conn.execute(
            "SELECT result FROM command_receipts WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is not None:
            logger.info("command receipt replayed id=%s aggregate=%s", command_id, aggregate)
            return (json.loads(row["result"]) if row["result"] is not None else None), True
        result = fn(*args, **kwargs)
        now = self._clock()
        self._conn.execute(
            "INSERT INTO command_receipts(command_id, aggregate, result, created_at) "
            "VALUES(?, ?, ?, ?)",
            (command_id, aggregate, json.dumps(result), now),
        )
        logger.info("command receipt recorded id=%s aggregate=%s", command_id, aggregate)
        return result, False

    def execute_command(
        self,
        command_id: str,
        aggregate: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result, replayed = self.record_command(command_id, aggregate, fn, *args, **kwargs)
        if replayed:
            raise ReceiptReplayed(command_id, result)
        return result

    def has_receipt(self, command_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM command_receipts WHERE command_id = ?", (command_id,)
        ).fetchone()
        return row is not None

    # -------------------------------------------------- transactional outbox

    def commit_step(
        self,
        run_id: str,
        events: list[dict],
        effects: list[dict],
    ) -> list[int]:
        # One transaction: step events + side-effect enqueue (t3code
        # EventSink one-transaction persist). effects entries:
        # {node_id, action, payload, receipt_key?} — receipt_key defaults
        # to f"{run_id}:{node_id}:{action}".
        now = self._clock()
        effect_ids: list[int] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for ev in events:
                self._conn.execute(
                    "INSERT INTO run_events(run_id, type, payload, created_at) VALUES(?, ?, ?, ?)",
                    (
                        run_id,
                        str(ev.get("type", "")),
                        json.dumps(ev.get("payload", {})),
                        now,
                    ),
                )
            for fx in effects:
                receipt_key = str(
                    fx.get("receipt_key")
                    or f"{run_id}:{fx.get('node_id', '')}:{fx.get('action', '')}"
                )
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO outbox("
                    "receipt_key, run_id, node_id, action, payload, status, "
                    "available_at, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt_key,
                        run_id,
                        str(fx.get("node_id", "")),
                        str(fx.get("action", "")),
                        json.dumps(fx.get("payload", {})),
                        PENDING,
                        now,
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    effect_ids.append(int(cur.lastrowid))
                else:
                    logger.info("outbox effect deduped receipt_key=%s", receipt_key)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        if effects:
            logger.info(
                "step committed run=%s events=%d effects=%d", run_id, len(events), len(effect_ids)
            )
        return effect_ids

    def _row_to_effect(self, row: sqlite3.Row) -> Effect:
        return Effect(
            id=row["id"],
            receipt_key=row["receipt_key"],
            run_id=row["run_id"],
            node_id=row["node_id"],
            action=row["action"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            status=row["status"],
            attempts=row["attempts"],
            available_at=row["available_at"],
            last_error=row["last_error"],
        )

    def claim_next(self) -> Effect | None:
        # Serial claim with a lease: at-least-once delivery; a crashed
        # worker's lease expires and reconcile() requeues the effect.
        now = self._clock()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM outbox WHERE status = ? AND available_at <= ? ORDER BY id LIMIT 1",
                (PENDING, now),
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            self._conn.execute(
                "UPDATE outbox SET status = ?, lease_expires = ?, attempts = attempts + 1, "
                "updated_at = ? WHERE id = ?",
                (IN_FLIGHT, now + self.lease_seconds, now, row["id"]),
            )
            self._conn.execute("COMMIT")
            effect = self._row_to_effect(row)
            effect.status = IN_FLIGHT
            effect.attempts = row["attempts"] + 1
            logger.info(
                "effect claimed id=%s action=%s run=%s attempts=%d",
                effect.id,
                effect.action,
                effect.run_id,
                effect.attempts,
            )
            return effect
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def settle(
        self,
        effect_id: int,
        ok: bool,
        error: str | None = None,
        backoff_seconds: float = 0.0,
    ) -> None:
        # Persisted backoff: a failed effect returns to pending with
        # available_at in the future — never an in-memory retry timer.
        now = self._clock()
        if ok:
            self._conn.execute(
                "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?",
                (DONE, now, effect_id),
            )
            logger.info("effect done id=%s", effect_id)
            return
        available_at = now + max(0.0, backoff_seconds)
        self._conn.execute(
            "UPDATE outbox SET status = ?, available_at = ?, last_error = ?, updated_at = ? "
            "WHERE id = ?",
            (PENDING, available_at, error, now, effect_id),
        )
        logger.warning(
            "effect failed id=%s backoff=%.1fs error=%s", effect_id, backoff_seconds, error
        )

    # -------------------------------------------------- startup reconcile

    def reconcile(self, orphaned_run_ids: list[str] | None = None) -> ReconcileReport:
        # Boot reconcile (t3code ProviderRuntimeRecoveryService): expired
        # in-flight leases requeue in delivery order; effects of orphaned
        # runs retire; callers mark orphaned runs' honest terminal state
        # on their own run registry.
        now = self._clock()
        requeued = 0
        retired = 0
        orphaned = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "UPDATE outbox SET status = ?, updated_at = ? "
                "WHERE status = ? AND lease_expires < ?",
                (PENDING, now, IN_FLIGHT, now),
            )
            requeued = cur.rowcount
            if orphaned_run_ids:
                for run_id in orphaned_run_ids:
                    cur = self._conn.execute(
                        "UPDATE outbox SET status = ?, last_error = 'run orphaned', "
                        "updated_at = ? WHERE run_id = ? AND status IN (?, ?)",
                        (RETIRED, now, run_id, PENDING, IN_FLIGHT),
                    )
                    retired += cur.rowcount
            orphaned = len(orphaned_run_ids or [])
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        report = ReconcileReport(requeued, retired, orphaned)
        if requeued or retired or orphaned:
            logger.info("%s", report)
        return report

    # -------------------------------------------------- inspection

    def effect(self, effect_id: int) -> Effect | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (effect_id,)).fetchone()
        return self._row_to_effect(row) if row is not None else None

    def effect_by_receipt(self, receipt_key: str) -> Effect | None:
        row = self._conn.execute(
            "SELECT * FROM outbox WHERE receipt_key = ?", (receipt_key,)
        ).fetchone()
        return self._row_to_effect(row) if row is not None else None

    def effects_for_run(self, run_id: str) -> list[Effect]:
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [self._row_to_effect(r) for r in rows]

    def events_for_run(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT type, payload, created_at FROM run_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            {"type": r["type"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
            for r in rows
        ]

    def pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE status = ?", (PENDING,)
        ).fetchone()
        return int(row["n"])
