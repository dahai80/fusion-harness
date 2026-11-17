"""HumanGate: async human-approval hook + sink (issue #44).

Long-running sessions must not block on human availability. A question is
emitted with payload + reply schema to a persistent on-disk queue; the
asking session either parks (`policy="block"`, awaited) or continues
(`policy="continue"`, reply injected later as the session's next turn).
Gate state survives harness restart — pending questions and already-given
but unconsumed replies both reload. Reply validation is a deliberate
JSON-schema subset (required + per-key types); an invalid reply is
rejected loudly and the question stays pending. Zero model calls; no
engine-loop changes — the gate is a new sink kind beside the existing
permission sinks.
"""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_POLICIES = ("block", "continue")
_QUESTION_STATUSES = ("pending", "answered", "cancelled")

_REPLY_TYPES = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "list": list,
    "array": list,
    "dict": dict,
    "object": dict,
}


class HumanGateError(ValueError):
    """Malformed question, reply, or policy."""


class ReplyValidationError(HumanGateError):
    """Reply does not match the question's reply schema."""


def validate_reply(answer: Any, reply_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a reply dict against the schema subset: {"required": [...],
    "properties": {key: type-name}}. Returns the (coerced-free) answer dict
    or raises ReplyValidationError naming the failed key."""
    if not isinstance(answer, dict):
        raise ReplyValidationError("reply must be a dict, got %s" % type(answer).__name__)
    properties = reply_schema.get("properties", {}) if isinstance(reply_schema, dict) else {}
    required = reply_schema.get("required", []) if isinstance(reply_schema, dict) else []
    for key in required:
        if key not in answer:
            raise ReplyValidationError("reply missing required key '%s'" % key)
    for key, value in answer.items():
        expected = properties.get(key)
        if expected is None:
            continue
        python_type = _REPLY_TYPES.get(str(expected))
        if python_type is None:
            raise ReplyValidationError("unknown reply type %r for key '%s'" % (expected, key))
        if isinstance(value, bool) and python_type not in (bool, (int, float)):
            raise ReplyValidationError("reply key '%s' expected %s, got bool" % (key, expected))
        if not isinstance(value, python_type):
            raise ReplyValidationError(
                "reply key '%s' expected %s, got %s" % (key, expected, type(value).__name__)
            )
    return answer


@dataclass
class HumanQuestion:
    id: str
    session_id: str
    question: str
    payload: Dict[str, Any] = field(default_factory=dict)
    reply_schema: Dict[str, Any] = field(default_factory=dict)
    policy: str = "block"
    status: str = "pending"
    reply: Optional[Dict[str, Any]] = None
    created_at: float = 0.0
    answered_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.id or not self.session_id or not self.question:
            raise HumanGateError("id, session_id, and question are required")
        if self.policy not in _POLICIES:
            raise HumanGateError(
                "unknown policy: %s (allowed: %s)" % (self.policy, "|".join(_POLICIES))
            )
        if self.status not in _QUESTION_STATUSES:
            raise HumanGateError("unknown status: %s" % self.status)


class HumanGate:
    """Persistent async human-approval queue. One JSON file, atomic writes,
    in-process waiters via asyncio events; restart-safe by construction."""

    def __init__(self, state_dir: str, bus=None, clock: Optional[Any] = None) -> None:
        self.state_dir = state_dir
        self.bus = bus
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._questions: Dict[str, HumanQuestion] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._path = os.path.join(state_dir, "human_gate_queue.json")
        os.makedirs(state_dir, exist_ok=True)
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for data in raw:
                try:
                    q = HumanQuestion(**data)
                except (HumanGateError, TypeError) as exc:
                    logger.warning("skipping corrupt queue entry: %s", exc)
                    continue
                self._questions[q.id] = q
            logger.info("human gate queue restored count=%d", len(self._questions))
        except (OSError, ValueError) as exc:
            logger.error("human gate queue load failed: %s — starting empty", exc)

    def _persist(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([asdict(q) for q in self._questions.values()], fh, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ---------- asking ----------

    def ask(
        self,
        session_id: str,
        question: str,
        payload: Optional[Dict[str, Any]] = None,
        reply_schema: Optional[Dict[str, Any]] = None,
        policy: str = "block",
    ) -> HumanQuestion:
        q = HumanQuestion(
            id="hq_%s" % uuid.uuid4().hex[:8],
            session_id=session_id,
            question=question,
            payload=payload or {},
            reply_schema=reply_schema or {},
            policy=policy,
            created_at=self._clock(),
        )
        with self._lock:
            self._questions[q.id] = q
            self._events[q.id] = asyncio.Event()
            self._persist()
        self._emit(
            "human_gate.question", {"id": q.id, "session": session_id, "policy": policy}, session_id
        )
        logger.info("human question queued id=%s session=%s policy=%s", q.id, session_id, policy)
        return q

    async def wait(
        self, question_id: str, timeout: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Block-policy park point: resolves with the reply when it arrives.
        An already-answered (or answered-before-restart) question resolves
        immediately — answers are never lost, only consumed once."""
        with self._lock:
            q = self._questions.get(question_id)
            if q is None:
                raise HumanGateError("unknown question: %s" % question_id)
            if q.status == "answered":
                return q.reply
            if q.status == "cancelled":
                return None
            event = self._events.setdefault(question_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        with self._lock:
            q = self._questions[question_id]
            return q.reply if q.status == "answered" else None

    # ---------- replying ----------

    def reply(self, question_id: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            q = self._questions.get(question_id)
            if q is None:
                raise HumanGateError("unknown question: %s" % question_id)
            if q.status != "pending":
                raise HumanGateError("question %s already %s" % (question_id, q.status))
            validated = validate_reply(answer, q.reply_schema)
            q.reply = validated
            q.status = "answered"
            q.answered_at = self._clock()
            self._persist()
            event = self._events.get(question_id)
        if event is not None:
            event.set()
        self._emit(
            "human_gate.reply",
            {"id": question_id, "session": q.session_id, "status": "answered"},
            q.session_id,
        )
        logger.info("human question answered id=%s session=%s", question_id, q.session_id)
        return {"session_id": q.session_id, "reply": validated}

    def cancel(self, question_id: str) -> bool:
        with self._lock:
            q = self._questions.get(question_id)
            if q is None or q.status != "pending":
                return False
            q.status = "cancelled"
            self._persist()
            event = self._events.get(question_id)
        if event is not None:
            event.set()
        self._emit(
            "human_gate.reply",
            {"id": question_id, "session": q.session_id, "status": "cancelled"},
            q.session_id,
        )
        logger.info("human question cancelled id=%s", question_id)
        return True

    # ---------- inspection ----------

    def pending(self, session_id: Optional[str] = None) -> List[HumanQuestion]:
        with self._lock:
            return [
                q
                for q in self._questions.values()
                if q.status == "pending" and (session_id is None or q.session_id == session_id)
            ]

    def take_reply(self, question_id: str) -> Optional[Dict[str, Any]]:
        """Continue-policy consumption: returns the reply and marks the
        question consumed (removes it from the store). Idempotent-safe —
        second take returns None."""
        with self._lock:
            q = self._questions.get(question_id)
            if q is None or q.status != "answered":
                return None
            reply = q.reply
            del self._questions[question_id]
            self._persist()
        self._events.pop(question_id, None)
        logger.info("human reply consumed id=%s session=%s", question_id, q.session_id)
        return reply

    def _emit(self, event: str, payload: Dict[str, Any], session_id: str = "") -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit("human_gate", event, payload, session_id)
        except Exception as exc:
            logger.warning("human gate emit failed: %s", exc)


__all__ = [
    "HumanGate",
    "HumanGateError",
    "HumanQuestion",
    "ReplyValidationError",
    "validate_reply",
]
