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
import uuid
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
    lineage_id TEXT NOT NULL DEFAULT '',
    UNIQUE(session_id, seq)
)
"""


class CheckpointError(ValueError):
    """Malformed or missing checkpoint / lineage operation."""


@dataclass
class CheckpointState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    budget_spent_tokens: int = 0
    permission_mode: str = ""
    outbox_offset: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    lineage_id: str = ""
    parent_checkpoint: Dict[str, Any] = field(default_factory=dict)

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
            lineage_id=str(data.get("lineage_id") or ""),
            parent_checkpoint=dict(data.get("parent_checkpoint") or {}),
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
        try:
            # Pre-lineage stores lack the column; migrate in place.
            self._conn.execute(
                "ALTER TABLE checkpoints ADD COLUMN lineage_id TEXT NOT NULL DEFAULT ''"
            )
            logger.info("checkpoint store migrated: added lineage_id column")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()
        logger.info("checkpoint store ready path=%s", path)

    def save(self, session_id: str, state: CheckpointState, seq: Optional[int] = None) -> int:
        if seq is None:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row[0])
        lineage = state.lineage_id or session_id
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (session_id, seq, created_at, state, lineage_id) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, self._clock(), state.to_json(), lineage),
        )
        self._conn.commit()
        logger.info(
            "checkpoint saved session=%s seq=%d msgs=%d lineage=%s",
            session_id,
            seq,
            len(state.messages),
            lineage,
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
        # Lineage root (seq 1) is never pruned — the fork/resume anchor.
        cur = self._conn.execute(
            "DELETE FROM checkpoints WHERE session_id = ? AND seq <= ? AND seq > 1",
            (session_id, cutoff),
        )
        self._conn.commit()
        logger.info("checkpoints pruned session=%s removed=%d", session_id, cur.rowcount)
        return cur.rowcount

    def fork(
        self,
        session_id: str,
        seq: int,
        new_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Branch from a checkpoint into a NEW lineage (issue #74): the
        child session starts with an identical state copy; the original
        lineage stays resumable and untouched. Returns the fork descriptor
        {session_id, seq, lineage_id, parent_checkpoint, fork_chain}."""
        src = self.load(session_id, seq)
        if src is None:
            raise CheckpointError("no checkpoint %s@%s to fork" % (session_id, seq))
        child_id = new_session_id or "%s_fork_%s" % (session_id, uuid.uuid4().hex[:8])
        parent_lineage = src.lineage_id or session_id
        chain = self._walk_fork_chain(session_id)
        parent_ref = {
            "session_id": session_id,
            "seq": seq,
            "lineage_id": parent_lineage,
        }
        child_meta = dict(src.metadata)
        child_meta["fork_chain"] = chain + [parent_ref]
        child = CheckpointState(
            messages=list(src.messages),
            budget_spent_tokens=src.budget_spent_tokens,
            permission_mode=src.permission_mode,
            outbox_offset=src.outbox_offset,
            metadata=child_meta,
            lineage_id=child_id,
            parent_checkpoint=parent_ref,
        )
        child_seq = self.save(child_id, child)
        logger.info(
            "checkpoint forked parent=%s@%d child=%s lineage=%s",
            session_id,
            seq,
            child_id,
            child_id,
        )
        return {
            "session_id": child_id,
            "seq": child_seq,
            "lineage_id": child_id,
            "parent_checkpoint": parent_ref,
            "fork_chain": list(child_meta["fork_chain"]),
        }

    def _walk_fork_chain(self, session_id: str) -> List[Dict[str, Any]]:
        """Ancestors of a session, root first. Walks each ancestor session's
        root checkpoint (seq 1, the fork point) — intermediate saves on a
        forked session must not lose the chain."""
        chain: List[Dict[str, Any]] = []
        seen = set()
        cur = session_id
        while cur and cur not in seen:
            seen.add(cur)
            root = self.load(cur, 1)
            if root is None:
                break
            parent = root.parent_checkpoint
            if not parent or not parent.get("session_id"):
                break
            chain.insert(
                0,
                {
                    "session_id": parent["session_id"],
                    "seq": int(parent.get("seq") or 0),
                    "lineage_id": parent.get("lineage_id") or parent["session_id"],
                },
            )
            cur = parent["session_id"]
        return chain

    def fork_chain(self, session_id: str) -> List[Dict[str, Any]]:
        """Full fork chain for a session, root first: walks parent_checkpoint
        links from the session's root checkpoint up to its lineage root."""
        return self._walk_fork_chain(session_id)

    def lineage_checkpoints(self, lineage_id: str) -> List[Dict[str, Any]]:
        """All checkpoints belonging to one lineage (lineage_id column),
        ordered by session then seq. Pre-lineage rows (empty lineage_id)
        count as their own session's lineage."""
        rows = self._conn.execute(
            "SELECT session_id, seq, created_at FROM checkpoints WHERE lineage_id = ? OR (lineage_id = '' AND session_id = ?) ORDER BY session_id, seq",
            (lineage_id, lineage_id),
        ).fetchall()
        return [{"session_id": r[0], "seq": int(r[1]), "created_at": float(r[2])} for r in rows]

    def close(self) -> None:
        self._conn.close()


__all__ = ["CheckpointError", "CheckpointState", "CheckpointStore"]
