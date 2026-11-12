"""Resumable checkpoints (plan P2-2, gap G4).

Durable checkpoint records at tool-call boundaries: context messages, budget
state, permission mode, outbox offset. Resume rebuilds session state without
re-executing committed tool effects — the outbox's receipt replay makes effect
re-execution idempotent, so a checkpoint restore + redelivery is safe by
construction. Stdlib sqlite, same store style as the outbox.
"""

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    UNIQUE(session_id, seq)
)
"""


@dataclass
class CheckpointState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    budget_spent_tokens: int = 0
    permission_mode: str = ""
    outbox_offset: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CheckpointState":
        data = json.loads(raw)
        return cls(
            messages=list(data.get("messages") or []),
            budget_spent_tokens=int(data.get("budget_spent_tokens") or 0),
            permission_mode=str(data.get("permission_mode") or ""),
            outbox_offset=int(data.get("outbox_offset") or 0),
            metadata=dict(data.get("metadata") or {}),
        )


class CheckpointStore:
    def __init__(
        self,
        path: str = ":memory:",
        conn: Optional[sqlite3.Connection] = None,
        clock: Any = time.time,
    ) -> None:
        self._conn = conn or sqlite3.connect(path, check_same_thread=False)
        self._clock = clock
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("checkpoint store ready path=%s", path)

    def save(self, session_id: str, state: CheckpointState, seq: Optional[int] = None) -> int:
        if seq is None:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row[0])
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (session_id, seq, created_at, state) VALUES (?, ?, ?, ?)",
            (session_id, seq, self._clock(), state.to_json()),
        )
        self._conn.commit()
        logger.info(
            "checkpoint saved session=%s seq=%d msgs=%d", session_id, seq, len(state.messages)
        )
        return seq

    def latest(self, session_id: str) -> Optional[CheckpointState]:
        row = self._conn.execute(
            "SELECT state FROM checkpoints WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return CheckpointState.from_json(row[0])

    def load(self, session_id: str, seq: int) -> Optional[CheckpointState]:
        row = self._conn.execute(
            "SELECT state FROM checkpoints WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        ).fetchone()
        if row is None:
            return None
        return CheckpointState.from_json(row[0])

    def list_checkpoints(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, created_at FROM checkpoints WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [{"seq": int(r[0]), "created_at": float(r[1])} for r in rows]

    def prune(self, session_id: str, keep_last: int = 10) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        max_seq = int(row[0])
        cutoff = max_seq - keep_last
        if cutoff <= 0:
            return 0
        cur = self._conn.execute(
            "DELETE FROM checkpoints WHERE session_id = ? AND seq <= ?",
            (session_id, cutoff),
        )
        self._conn.commit()
        logger.info("checkpoints pruned session=%s removed=%d", session_id, cur.rowcount)
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()


__all__ = ["CheckpointState", "CheckpointStore"]
