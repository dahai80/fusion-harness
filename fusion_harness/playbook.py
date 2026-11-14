"""Playbook structured append protocol (issue #28, P5).

Evolving playbooks (learned rules, curated context) are updated via
structured increments — add/update/delete ops with provenance — applied
append-first, never free-form rewrite. Targets two failure modes (ACE,
arXiv 2510.04618, +10.6% agents): brevity bias (summarization drops domain
detail) and context collapse (iterative rewriting erodes information).
Every materialized entry is traceable to its originating episode(s).
Provenance feeds fusion-memory P1 admission metadata (consumer side).
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PlaybookOp:
    op: str  # add | update | delete
    section: str
    content: str
    episodes: List[str] = field(default_factory=list)
    target_id: Optional[str] = None  # required for update/delete
    op_id: str = ""
    ts: float = 0.0

    def __post_init__(self) -> None:
        if self.op not in ("add", "update", "delete"):
            raise ValueError("unknown playbook op: %s" % self.op)
        if not self.section or not isinstance(self.section, str):
            raise ValueError("playbook op section required")
        if self.op in ("add", "update") and not (self.content or "").strip():
            raise ValueError("op %s requires non-empty content" % self.op)
        if self.op in ("update", "delete") and not self.target_id:
            raise ValueError("op %s requires target_id" % self.op)
        if self.op == "add" and self.target_id:
            raise ValueError("add must not carry target_id")
        if not self.episodes:
            raise ValueError("provenance required: at least one originating episode")
        if not self.op_id:
            self.op_id = "op-%d" % int((self.ts or time.time()) * 1e6)
        if not self.ts:
            self.ts = time.time()


@dataclass
class PlaybookEntry:
    entry_id: str
    section: str
    content: str
    episodes: List[str]
    revision: int
    deleted: bool = False


class Playbook:
    """Append-only op log + materialized view. The log is the source of
    truth; render() folds it deterministically. History is never rewritten."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ops: List[PlaybookOp] = []
        self._next = 0

    def apply(self, op: PlaybookOp) -> PlaybookEntry:
        self.ops.append(op)
        self._next += 1
        entry_id = op.target_id or "%s-e%d" % (self.name, self._next)
        entry = PlaybookEntry(
            entry_id=entry_id,
            section=op.section,
            content=op.content,
            episodes=list(op.episodes),
            revision=self._next,
        )
        logger.info("playbook op applied book=%s op=%s section=%s", self.name, op.op, op.section)
        return entry

    def render(self) -> str:
        """Materialize the current view by folding the op log in order.
        add creates an entry, update rewrites content (+keeps merged
        episodes), delete marks deleted (tombstone — history preserved)."""
        entries: Dict[str, PlaybookEntry] = {}
        order: List[str] = []
        for n, op in enumerate(self.ops, start=1):
            if op.op == "add":
                entry_id = "%s-e%d" % (self.name, n)
                entries[entry_id] = PlaybookEntry(
                    entry_id=entry_id,
                    section=op.section,
                    content=op.content,
                    episodes=list(op.episodes),
                    revision=n,
                )
                order.append(entry_id)
            elif op.op == "update":
                target = entries.get(op.target_id or "")
                if target is None or target.deleted:
                    logger.warning(
                        "playbook update of missing/deleted entry id=%s skipped", op.target_id
                    )
                    continue
                target.content = op.content
                for ep in op.episodes:
                    if ep not in target.episodes:
                        target.episodes.append(ep)
                target.revision = n
            elif op.op == "delete":
                target = entries.get(op.target_id or "")
                if target is not None:
                    target.deleted = True
                    target.revision = n
        lines = ["# Playbook: %s" % self.name]
        for entry_id in order:
            e = entries[entry_id]
            if e.deleted:
                continue
            lines.append(
                "[%s] (%s) %s [episodes: %s]"
                % (e.section, e.entry_id, e.content, ",".join(e.episodes))
            )
        return "\n".join(lines)

    def trace(self, entry_id: str) -> List[str]:
        """All originating episodes for an entry (add + every update)."""
        episodes: List[str] = []
        n = 0
        for op in self.ops:
            if op.op == "add":
                n += 1
                current_id = "%s-e%d" % (self.name, n)
                if current_id == entry_id:
                    episodes.extend(op.episodes)
            elif op.op == "update" and op.target_id == entry_id:
                episodes.extend(op.episodes)
        seen: List[str] = []
        for ep in episodes:
            if ep not in seen:
                seen.append(ep)
        return seen


__all__ = ["Playbook", "PlaybookOp", "PlaybookEntry"]
