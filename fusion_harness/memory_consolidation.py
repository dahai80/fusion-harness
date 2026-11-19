"""Memory consolidation op set at the write gate (issue #100).

Design: docs/design/memory-consolidation-ops-100.md. The write gate is no
longer binary (write / quarantine): each candidate memory is decided
against its retrieval neighbors as ADD / UPDATE / DELETE / NOOP, with
tombstones instead of deletion and bounded retroactive updates. Store ops
themselves belong to fusion-memory; this kernel owns the decision policy
and the regression suite (evals/memory_bench.py).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class MemoryOp(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"


OP_ADD = MemoryOp.ADD
OP_UPDATE = MemoryOp.UPDATE
OP_DELETE = MemoryOp.DELETE
OP_NOOP = MemoryOp.NOOP

DUP_SIMILARITY = 0.92
NEIGHBOR_FLOOR = 0.5
MAX_RETROACTIVE = 3

_RETRACT_RE = re.compile(r"\b(retract|retracted|forget|disregard|withdraw)\b", re.I)
# "X is A" vs "X is B" contradiction probe over short factual lines.
_FACT_RE = re.compile(r"^([A-Za-z0-9_. -]{2,40}?)\s+(?:is|=|was)\s+(.+)$", re.I)


def similarity(a: str, b: str) -> float:
    """Deterministic kernel-side heuristic: token-set Jaccard on
    lowercase words (len >= 2). Exact/near-exact offline; a real
    deployment swaps in the MemorySink's own ranking."""
    wa = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) >= 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) >= 2}
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    return inter / len(wa | wb)


def containment(a: str, b: str) -> float:
    """Overlap over the smaller token set — 1.0 when one text's words are
    a subset of the other's. Better than Jaccard at near-duplicate
    detection when one memory extends another with extra detail."""
    wa = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) >= 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) >= 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


@dataclass
class Neighbor:
    key: str
    payload: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    @property
    def tombstoned(self) -> bool:
        return bool(self.metadata.get("tombstoned"))


@dataclass
class ConsolidationDecision:
    op: str
    reason: str
    neighbor: Optional[Neighbor] = None
    similarity: float = 0.0

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "reason": self.reason,
            "neighbor_key": self.neighbor.key if self.neighbor else None,
            "similarity": round(self.similarity, 6),
        }


def _contradicts(candidate: str, neighbor_payload: str) -> bool:
    ma = _FACT_RE.match(candidate.strip())
    mb = _FACT_RE.match(neighbor_payload.strip())
    if not ma or not mb:
        return False
    if ma.group(1).strip().lower() != mb.group(1).strip().lower():
        return False
    va = ma.group(2).strip().rstrip(".")
    vb = mb.group(2).strip().rstrip(".")
    return va.lower() != vb.lower()


def _subject_of(payload: str) -> str:
    m = _FACT_RE.match(payload.strip())
    return m.group(1).strip().lower() if m else ""


def decide(candidate: str, neighbors: Sequence[Neighbor]) -> ConsolidationDecision:
    """Deterministic decision policy (LLM-replaceable via injection
    upstream). Order: retraction -> contradiction -> near-duplicate ->
    neighbor floor. Scores use token containment (a memory extended with
    extra detail is still a near-duplicate)."""
    live = [n for n in neighbors if not n.tombstoned]
    for n in live:
        if _RETRACT_RE.search(candidate) and containment(candidate, n.payload) >= NEIGHBOR_FLOOR:
            return ConsolidationDecision(
                OP_DELETE,
                "candidate retracts existing memory",
                n,
                containment(candidate, n.payload),
            )
    for n in live:
        if _contradicts(candidate, n.payload):
            return ConsolidationDecision(
                OP_UPDATE, "same subject, conflicting value — supersede with tombstone", n, 1.0
            )
    if live:
        best = max(live, key=lambda n: containment(candidate, n.payload))
        score = containment(candidate, best.payload)
        if score >= DUP_SIMILARITY:
            return ConsolidationDecision(
                OP_UPDATE, "near-duplicate of existing memory", best, score
            )
        if score < NEIGHBOR_FLOOR:
            return ConsolidationDecision(OP_ADD, "no meaningful neighbor", best, score)
        return ConsolidationDecision(
            OP_NOOP, "neighbor in the gray zone — do not write", best, score
        )
    return ConsolidationDecision(OP_ADD, "no neighbors at all", None, 0.0)


class MemoryConsolidator:
    """Decides each candidate against retrieval neighbors and applies the
    op to the injected sink (MemorySink-like: store(key, payload,
    metadata)). Tombstones are metadata, never deletion."""

    def __init__(self, sink, source=None, lifecycle_bus=None, session_id: str = ""):
        self.sink = sink
        self.source = source if source is not None else sink
        self.lifecycle_bus = lifecycle_bus
        self.session_id = session_id
        self.stats = {
            "candidates": 0,
            "adds": 0,
            "updates": 0,
            "deletes": 0,
            "noops": 0,
            "retroactive": 0,
        }

    def retrieve_neighbors(self, candidate: str, limit: int = 5) -> List[Neighbor]:
        try:
            raw = self.source.retrieve(candidate, limit=limit) or []
        except Exception as e:
            logger.warning("neighbor retrieval failed: %s", e)
            return []
        out = []
        for item in raw:
            out.append(
                Neighbor(
                    key=str(item.get("key") or ""),
                    payload=str(item.get("payload") or item.get("content") or ""),
                    metadata=dict(item.get("metadata") or {}),
                    score=float(
                        item.get("score") or similarity(candidate, str(item.get("payload") or ""))
                    ),
                )
            )
        return out

    def _emit(self, payload: dict) -> None:
        if self.lifecycle_bus is None:
            return
        try:
            self.lifecycle_bus.emit(
                "agent", "memory_consolidation", payload, session_id=self.session_id
            )
            logger.info("memory consolidation event emitted")
        except Exception as e:
            logger.warning("memory consolidation emit failed: %s", e)

    def consolidate(self, candidate: str, scope: str = "memory") -> ConsolidationDecision:
        """One candidate through the gate. Returns the decision taken."""
        self.stats["candidates"] += 1
        neighbors = self.retrieve_neighbors(candidate)
        decision = decide(candidate, neighbors)
        key = None
        try:
            if decision.op == OP_ADD:
                key = "%s:%s:%d" % (scope, self.session_id, int(time.time() * 1000))
                self.sink.store(key=key, payload=candidate, metadata={"origin": "derived"})
                self.stats["adds"] += 1
            elif decision.op == OP_UPDATE:
                n = decision.neighbor
                key = n.key
                merged = "%s\n[update %s] %s" % (n.payload, time.strftime("%Y-%m-%d"), candidate)
                meta = dict(n.metadata)
                if decision.reason.startswith("same subject"):
                    meta["tombstoned"] = True
                    meta["superseded_by"] = "%s:superseded" % n.key
                    self.sink.store(
                        key=meta["superseded_by"], payload=merged, metadata={"origin": "derived"}
                    )
                else:
                    self.sink.store(key=key, payload=merged, metadata=meta)
                    self._retroactive_refresh(candidate, exclude=n.key)
                self.stats["updates"] += 1
            elif decision.op == OP_DELETE:
                key = decision.neighbor.key
                meta = dict(decision.neighbor.metadata)
                meta["tombstoned"] = True
                meta["tombstone_reason"] = decision.reason
                self.sink.store(key=key, payload=decision.neighbor.payload, metadata=meta)
                self.stats["deletes"] += 1
            else:
                self.stats["noops"] += 1
        except Exception as e:
            logger.warning("consolidation sink op failed op=%s err=%s", decision.op, e)
            return ConsolidationDecision(
                OP_NOOP, "sink op failed: %s" % e, decision.neighbor, decision.similarity
            )
        self._emit(
            {
                "op": decision.op,
                "candidate_id": key,
                "neighbor_ids": [n.key for n in neighbors],
                "similarity": round(decision.similarity, 6),
                "reason": decision.reason,
            }
        )
        logger.info(
            "memory decision op=%s reason=%s neighbors=%d",
            decision.op,
            decision.reason,
            len(neighbors),
        )
        return decision

    def _retroactive_refresh(self, candidate: str, exclude: str) -> None:
        """A-MEM-style bounded evolution: refresh description/tags of at
        most k=3 live neighbors, one hop, never tombstoned entries."""
        for n in self.retrieve_neighbors(candidate, limit=MAX_RETROACTIVE + 1):
            if n.key == exclude or n.tombstoned:
                continue
            if n.score < NEIGHBOR_FLOOR:
                continue
            meta = dict(n.metadata)
            meta["last_refreshed_by"] = candidate[:120]
            try:
                self.sink.store(key=n.key, payload=n.payload, metadata=meta)
                self.stats["retroactive"] += 1
            except Exception as e:
                logger.warning("retroactive refresh failed key=%s err=%s", n.key, e)
            if self.stats["retroactive"] >= MAX_RETROACTIVE:
                break


def is_retrievable(item: Dict[str, Any]) -> bool:
    """Tombstoned memories are NOT retrievable (kernel-side convention the
    store alignment cross-repo issue will encode in fusion-memory)."""
    return not bool((item.get("metadata") or {}).get("tombstoned"))
