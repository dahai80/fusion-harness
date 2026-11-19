"""Proactive consolidation: MEM1 state rewrite + AgentFold verbs (issue #99).

Design: docs/design/proactive-consolidation-99.md. FOLD compresses a
resolved episode into a citable summary backed by the immutable
compaction archive (reversible via rehydration); DROP discards content
entirely (logged, never reversible; retention-list content is refused);
REWRITE merges the bounded per-turn state under a hard token cap and is
refused — never silently truncated — when the merge exceeds it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from fusion_harness.compaction_archive import cite_link
from fusion_harness.compactor import RetentionList

logger = logging.getLogger(__name__)

VERB_FOLD = "FOLD"
VERB_DROP = "DROP"
VERB_REWRITE = "REWRITE"

STATE_MARKER = "consolidation_state: true"


class ConsolidationError(ValueError):
    """A verb was refused — refusal is the contract, never silent trims."""


@dataclass
class ConsolidationConfig:
    state_cap_tokens: int = 2048
    enable_proactive: bool = False
    token_counter = None  # callable(str) -> int; default len(text) // 4


def _default_count(text: str) -> int:
    return max(1, len(text or "") // 4)


class ConsolidationLog:
    """Append-only JSONL audit log for consolidation verbs."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, entry: dict) -> None:
        entry = {"ts": time.time(), **entry}
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info("consolidation log appended verb=%s", entry.get("verb"))
        except OSError as e:
            logger.error("consolidation log write failed: %s", e)

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError as e:
                        logger.error("consolidation log corrupt line skipped: %s", e)
        return out


class Consolidator:
    """Executes FOLD/DROP/REWRITE over a message list. Host-invoked."""

    def __init__(
        self,
        config: ConsolidationConfig | None = None,
        archive=None,
        audit_log: ConsolidationLog | None = None,
        checkpoint_store=None,
        session_id: str = "",
        lifecycle_bus=None,
        summarizer=None,
    ):
        self.config = config or ConsolidationConfig()
        if not self.config.enable_proactive:
            raise ConsolidationError(
                "proactive consolidation disabled (ConsolidationConfig.enable_proactive)"
            )
        self.archive = archive
        self.audit_log = audit_log
        self.checkpoint_store = checkpoint_store
        self.session_id = session_id
        self.lifecycle_bus = lifecycle_bus
        self.summarizer = summarizer
        self.stats = {"folds": 0, "drops": 0, "rewrites": 0, "refused": 0, "undos": 0}

    def _count(self, text: str) -> int:
        counter = self.config.token_counter or _default_count
        return counter(text)

    def _log(self, entry: dict) -> None:
        if self.audit_log is not None:
            self.audit_log.append(entry)

    def _emit(self, event: str, payload: dict) -> None:
        if self.lifecycle_bus is None:
            return
        try:
            self.lifecycle_bus.emit("agent", event, payload, session_id=self.session_id)
        except Exception as e:
            logger.warning("consolidation emit failed event=%s err=%s", event, e)

    def _protected_lines(self, messages: list[dict]) -> list[str]:
        rl = RetentionList.from_messages(messages)
        lines = []
        for bucket in (rl.todos, rl.interfaces, rl.constraints, rl.active_tasks):
            lines.extend(bucket)
        return lines

    @staticmethod
    def _scope_text(messages: list[dict], start: int, end: int) -> str:
        parts = []
        for m in messages[start:end]:
            c = m.get("content") if isinstance(m, dict) else None
            parts.append(c if isinstance(c, str) else str(c))
        return "\n".join(parts)

    def fold(self, messages: list[dict], start: int, end: int, summary: str) -> list[dict]:
        """Compress messages[start:end] into one citable summary message.

        Reversible: originals move to the archive; rehydrate the link to
        restore. A checkpoint is saved before folding when a store is
        wired, so a fork can resume from the pre-fold state.
        """
        if not (0 <= start < end <= len(messages)):
            self.stats["refused"] += 1
            raise ConsolidationError("FOLD scope out of bounds: %d:%d" % (start, end))
        if not summary or not summary.strip():
            self.stats["refused"] += 1
            raise ConsolidationError("FOLD requires a non-empty summary")
        if self.checkpoint_store is not None and self.session_id:
            try:
                state_messages = [dict(m) for m in messages]
                from fusion_harness.checkpoint import CheckpointState

                self.checkpoint_store.save(
                    self.session_id,
                    CheckpointState(messages=state_messages, permission_mode="pre_fold"),
                )
            except Exception as e:
                logger.warning("pre-fold checkpoint save failed: %s", e)
        link = None
        if self.archive is not None:
            link = self.archive.append(messages[start:end])[0]
            summary = cite_link(summary, link)
        folded = (
            messages[:start]
            + [{"role": "system", "content": "[fold] %s" % summary}]
            + messages[end:]
        )
        self.stats["folds"] += 1
        self._log(
            {
                "verb": VERB_FOLD,
                "scope": [start, end],
                "summary_ref": link,
                "state_cap": self.config.state_cap_tokens,
                "actor": "host",
            }
        )
        self._emit(
            "consolidation_applied",
            {"verb": VERB_FOLD, "scope": [start, end], "link": link},
        )
        return folded

    def drop(self, messages: list[dict], start: int, end: int, reason: str) -> list[dict]:
        """Discard messages[start:end]. Requires a reason; retention-list
        content is refused. Not reversible — the log records the act."""
        if not (0 <= start < end <= len(messages)):
            self.stats["refused"] += 1
            raise ConsolidationError("DROP scope out of bounds: %d:%d" % (start, end))
        if not reason or not reason.strip():
            self.stats["refused"] += 1
            raise ConsolidationError("DROP requires a reason string")
        protected = self._protected_lines(messages)
        scoped = self._scope_text(messages, start, end)
        for line in protected:
            if line and line in scoped:
                self.stats["refused"] += 1
                self._emit(
                    "consolidation_refused",
                    {"verb": VERB_DROP, "reason": "retention-list content in scope"},
                )
                raise ConsolidationError(
                    "DROP refused: retention-list content in scope (%r)" % line[:80]
                )
        remaining = messages[:start] + messages[end:]
        self.stats["drops"] += 1
        self._log(
            {
                "verb": VERB_DROP,
                "scope": [start, end],
                "reason": reason,
                "dropped_bytes": len(scoped),
                "state_cap": self.config.state_cap_tokens,
                "actor": "host",
            }
        )
        self._emit(
            "consolidation_applied",
            {"verb": VERB_DROP, "scope": [start, end], "reason": reason},
        )
        return remaining

    def rewrite(self, prev_state: str, observation: str) -> str:
        """MEM1 per-turn state merge under a hard token cap.

        Over cap -> ConsolidationError (caller falls back to FOLD);
        never silently truncated."""
        merged = self._merge(prev_state, observation)
        tokens = self._count(merged)
        if tokens > self.config.state_cap_tokens:
            self.stats["refused"] += 1
            self._emit(
                "consolidation_refused",
                {
                    "verb": VERB_REWRITE,
                    "reason": "state cap exceeded",
                    "tokens": tokens,
                    "cap": self.config.state_cap_tokens,
                },
            )
            raise ConsolidationError(
                "REWRITE refused: %d tokens > cap %d (fall back to FOLD)"
                % (tokens, self.config.state_cap_tokens)
            )
        self.stats["rewrites"] += 1
        self._log(
            {
                "verb": VERB_REWRITE,
                "state_tokens": tokens,
                "state_cap": self.config.state_cap_tokens,
                "actor": "host",
            }
        )
        return merged

    def _merge(self, prev_state: str, observation: str) -> str:
        if self.summarizer is not None:
            return self.summarizer(prev_state, observation)
        prev = (prev_state or "").strip()
        obs = (observation or "").strip()
        if not prev:
            return obs
        if not obs:
            return prev
        return "%s\n[update] %s" % (prev, obs)

    def undo_fold(self, folded: list[dict], index: int) -> list[dict]:
        """Rehydrate a folded message back into the original range."""
        if self.archive is None:
            raise ConsolidationError("undo_fold requires a wired archive")
        m = folded[index]
        content = m.get("content") if isinstance(m, dict) else None
        from fusion_harness.compaction_archive import extract_links

        links = extract_links(content if isinstance(content, str) else "")
        if not links:
            raise ConsolidationError("no archive link at message %d" % index)
        # The archived span replaces the stub verbatim.
        restored = folded[:index] + self.archive.resolve(links[0]) + folded[index + 1 :]
        self.stats["undos"] += 1
        self._log({"verb": "UNDO_FOLD", "index": index, "link": links[0], "actor": "host"})
        return restored
