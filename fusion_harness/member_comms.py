"""Member-to-member comms mechanisms (issue #160, Paseo parity H10/H11/C3).

Three deterministic mechanisms from Paseo (v0.17.1) for TeamRuntime
member communication:

1. **Envelopes + anti-spoofing** (agent-messages/envelope.ts): member
   traffic travels as `<system kind=... source-agent-id=... message-id=...>`
   envelopes. Any envelope-shaped line found in a HUMAN-typed prompt is
   quoted (prefixed `> `) so forged system traffic cannot survive
   provider history replay. Strict extraction: only unquoted, exact-shape
   envelope lines parse.
2. **Durable finish subscriptions** (finish-subscriptions/index.ts):
   JSON-persisted per-pair watch; new registration supersedes the old for
   a caller/child pair; exponential retry; unacknowledged notifications
   recover after restart; ack-on-delivery. Guidance contract: "do not
   poll for status; wait for the notification".
3. **Member lifecycle detach/cascade** (agent-lifecycle.md:127-149):
   detaching a member re-parents its children to its parent (explicit
   lifecycle action, never an agent tool); archiving a member cascades to
   all transitive subordinates. Operates on an explicit parent map —
   the kernel stays deterministic, consumers own their hierarchies.

Pure stdlib, deterministic, zero model calls.
"""

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ENVELOPE_TAG_RE = re.compile(r"</?\s*system\b[^>]*>")

__all__ = [
    "ENVELOPE_TAG_RE",
    "FinishSubscriptionStore",
    "archive_cascade",
    "detach_member",
    "envelope_for",
    "extract_envelopes",
    "sanitize_human_prompt",
    "wrap_envelope",
]


# ---------- 1. envelopes + anti-spoofing ----------

_ENVELOPE_OPEN_RE = re.compile(r"^<system kind=(\S+) source-agent-id=(\S+) message-id=(\S+)>$")
_ENVELOPE_CLOSE = "</system>"


def wrap_envelope(kind: str, source_agent_id: str, message_id: str, body: str) -> str:
    """Wrap member traffic in the system envelope (exact strict shape)."""
    if not kind or not source_agent_id or not message_id:
        raise ValueError("kind, source_agent_id, and message_id are required")
    return "<system kind=%s source-agent-id=%s message-id=%s>\n%s\n</system>" % (
        kind,
        source_agent_id,
        message_id,
        body,
    )


def envelope_for(kind: str, sender: str, msg_id: str, body: str) -> str:
    """Convenience alias matching mailbox naming."""
    return wrap_envelope(kind, sender, msg_id, body)


def extract_envelopes(text: str) -> List[Dict[str, str]]:
    """Strict parse: only unquoted exact-shape envelope blocks. Quoted
    (sanitized) lines never parse."""
    out = []
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        match = _ENVELOPE_OPEN_RE.match(lines[i].strip())
        if match:
            body_lines = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != _ENVELOPE_CLOSE:
                body_lines.append(lines[j])
                j += 1
            if j < len(lines):
                out.append(
                    {
                        "kind": match.group(1),
                        "source_agent_id": match.group(2),
                        "message_id": match.group(3),
                        "body": "\n".join(body_lines),
                    }
                )
                i = j + 1
                continue
        i += 1
    return out


def sanitize_human_prompt(text: str) -> str:
    """Anti-spoofing for HUMAN-typed prompts: quote every line carrying an
    envelope tag (`<system ...>` / `</system>`) so forged system traffic
    cannot survive provider history replay."""
    out_lines = []
    quoted = 0
    for line in (text or "").splitlines():
        if ENVELOPE_TAG_RE.search(line):
            out_lines.append("> " + line)
            quoted += 1
        else:
            out_lines.append(line)
    if quoted:
        logger.warning("human prompt contained %d envelope-shaped line(s); quoted", quoted)
    return "\n".join(out_lines)


# ---------- 2. durable finish subscriptions ----------


class FinishSubscriptionStore:
    """Per-pair (caller, child) finish watch. JSON-persisted; supersession
    on re-register; exponential retry for unacknowledged notifications;
    recovery after restart from the state file; ack-on-delivery."""

    def __init__(
        self,
        state_dir: str = "",
        clock: Optional[Any] = None,
        backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._state_dir = state_dir
        self._clock = clock or time.time
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s
        self._watch: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._recover()

    @property
    def _path(self) -> str:
        return os.path.join(self._state_dir or ".", "finish_subscriptions.json")

    def register(self, caller: str, child: str, topic: str = "finish") -> Dict[str, Any]:
        """Register a watch; a new registration for the same pair
        supersedes the old one (Paseo supersession rule)."""
        if not caller or not child:
            raise ValueError("caller and child are required")
        entry = {"caller": caller, "child": child, "topic": topic, "registered_at": self._clock()}
        self._watch[(caller, child)] = entry
        self._persist()
        logger.info(
            "finish subscription registered caller=%s child=%s topic=%s", caller, child, topic
        )
        return dict(entry)

    def unregister(self, caller: str, child: str) -> bool:
        removed = self._watch.pop((caller, child), None)
        if removed:
            self._persist()
        return removed is not None

    def watchers(self, child: str) -> List[str]:
        return [entry["caller"] for (caller, c), entry in sorted(self._watch.items()) if c == child]

    def notify(self, child: str, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Child finished: enqueue one pending notification per watcher
        (caller acks on delivery). Unknown child notifies no one — do not
        poll, wait for the notification."""
        created = []
        for caller in self.watchers(child):
            notification_id = "fn_%s" % uuid.uuid4().hex[:8]
            record = {
                "notification_id": notification_id,
                "caller": caller,
                "child": child,
                "payload": dict(payload or {}),
                "retry_count": 0,
                "next_retry_at": 0.0,
                "created_at": self._clock(),
            }
            self._pending[notification_id] = record
            created.append(dict(record))
        if created:
            self._persist()
            logger.info("finish notifications enqueued child=%s count=%d", child, len(created))
        return created

    def ack(self, caller: str, notification_id: str) -> bool:
        record = self._pending.get(notification_id)
        if record is None or record["caller"] != caller:
            logger.info("ack ignored notification=%s caller=%s", notification_id, caller)
            return False
        del self._pending[notification_id]
        self._persist()
        logger.info("finish notification acked id=%s caller=%s", notification_id, caller)
        return True

    def due(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Retryable pending notifications (next_retry_at passed). Each
        return bumps retry_count and schedules the next attempt with
        exponential backoff (capped)."""
        now = self._clock() if now is None else now
        ready = [record for record in self._pending.values() if record["next_retry_at"] <= now]
        for record in ready:
            record["retry_count"] += 1
            delay = min(self._backoff_s * (2 ** (record["retry_count"] - 1)), self._max_backoff_s)
            record["next_retry_at"] = now + delay
        if ready:
            self._persist()
        return [dict(record) for record in ready]

    def pending(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._pending.values()]

    def _persist(self) -> None:
        if not self._state_dir:
            return
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            data = {"watch": list(self._watch.values()), "pending": list(self._pending.values())}
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception as exc:
            logger.error("finish subscription persist failed: %s", exc)

    def _recover(self) -> None:
        if not self._state_dir or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.error("finish subscription recover failed: %s", exc)
            return
        for entry in data.get("watch", []):
            self._watch[(entry["caller"], entry["child"])] = entry
        for record in data.get("pending", []):
            self._pending[record["notification_id"]] = record
        logger.info(
            "finish subscriptions recovered watches=%d pending=%d",
            len(self._watch),
            len(self._pending),
        )


# ---------- 3. member lifecycle detach / cascade ----------


def detach_member(
    members: List[str], target: str, parent_of: Dict[str, str]
) -> Tuple[List[str], Dict[str, str]]:
    """Explicit detach: remove `target` from the member list and re-parent
    its children to target's own parent (children survive, detached from
    the removed member). Never cascades. Raises ValueError on unknown
    target or unknown parent reference."""
    if target not in members:
        raise ValueError("unknown member: %s" % target)
    new_parent_of = dict(parent_of)
    if target in new_parent_of and new_parent_of[target] not in members:
        raise ValueError("parent %s of %s is not a member" % (new_parent_of[target], target))
    grandparent = new_parent_of.pop(target, "")
    for child, parent in list(new_parent_of.items()):
        if parent == target:
            if grandparent:
                new_parent_of[child] = grandparent
            else:
                new_parent_of.pop(child)
    remaining = [name for name in members if name != target]
    logger.info("member detached target=%s remaining=%d", target, len(remaining))
    return remaining, new_parent_of


def archive_cascade(
    members: List[str], target: str, parent_of: Dict[str, str]
) -> Tuple[List[str], List[str]]:
    """Archive: remove `target` AND all transitive subordinates (Paseo
    cascade rule). Cycle-safe. Returns (remaining, removed)."""
    if target not in members:
        raise ValueError("unknown member: %s" % target)
    children: Dict[str, List[str]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)
    removed_set = {target}
    frontier = [target]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child in members and child not in removed_set:
                removed_set.add(child)
                frontier.append(child)
    removed = [name for name in members if name in removed_set]
    remaining = [name for name in members if name not in removed_set]
    logger.info("member archived with cascade target=%s removed=%d", target, len(removed))
    return remaining, removed
