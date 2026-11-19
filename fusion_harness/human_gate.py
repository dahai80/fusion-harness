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

from fusion_harness.checkpoint import CheckpointState

logger = logging.getLogger(__name__)

_POLICIES = ("block", "continue")

# Issue #125: explicit-checkpoint school for durable HITL pauses. When a
# CheckpointStore is wired (checkpoint_ref), every pending HumanQuestion is
# mirrored as a checkpoint entry with the `awaiting_human` state marker
# carrying the question payload; answer/cancel/expiry update it. Opt-in —
# unwired gate is unchanged (the JSON queue already survives restart).
AWAITING_HUMAN_MARKER = "awaiting_human"
_QUESTION_STATUSES = ("pending", "answered", "cancelled", "expired")

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
    requester: str = ""
    expires_at: float = 0.0

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

    def __init__(
        self,
        state_dir: str,
        bus=None,
        clock: Optional[Any] = None,
        checkpoint_ref=None,
    ) -> None:
        self.state_dir = state_dir
        self.bus = bus
        self.checkpoint_ref = checkpoint_ref
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

    def _expired(self, q: HumanQuestion) -> bool:
        return q.expires_at > 0 and self._clock() >= q.expires_at

    def _expire_if_due(self, q: HumanQuestion) -> bool:
        """Lazy expiry transition: a pending question past its deadline
        flips to 'expired' (persisted) — no sweeper needed."""
        if q.status == "pending" and self._expired(q):
            q.status = "expired"
            self._persist()
            logger.info("human question expired id=%s", q.id)
            return True
        return False

    def ask(
        self,
        session_id: str,
        question: str,
        payload: Optional[Dict[str, Any]] = None,
        reply_schema: Optional[Dict[str, Any]] = None,
        policy: str = "block",
        requester: str = "",
        ttl_s: Optional[float] = None,
    ) -> HumanQuestion:
        q = HumanQuestion(
            id="hq_%s" % uuid.uuid4().hex[:8],
            session_id=session_id,
            question=question,
            payload=payload or {},
            reply_schema=reply_schema or {},
            policy=policy,
            created_at=self._clock(),
            requester=requester,
            expires_at=(self._clock() + ttl_s) if ttl_s else 0.0,
        )
        with self._lock:
            self._questions[q.id] = q
            self._events[q.id] = asyncio.Event()
            self._persist()
        self._checkpoint_question(q, "pending")
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
            if q.status in ("cancelled", "expired"):
                return None
            self._expire_if_due(q)
            if q.status == "expired":
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
            self._expire_if_due(q)
            if q.status != "pending":
                raise HumanGateError("question %s already %s" % (question_id, q.status))
            validated = validate_reply(answer, q.reply_schema)
            q.reply = validated
            q.status = "answered"
            q.answered_at = self._clock()
            self._persist()
            self._checkpoint_question(q, "answered", reply=validated)
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
            self._checkpoint_question(q, "cancelled")
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

    # ---------- generic queue surface (issue #66) ----------

    def resolve(self, question_id: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        """Generic single-winner resolve for queue consumers: exactly one
        concurrent caller wins; everyone else gets resolved=False with a
        reason (never an exception, unlike reply()). Expired questions
        reject resolution. Reply validation is the same validate_reply."""
        with self._lock:
            q = self._questions.get(question_id)
            if q is None:
                return {"resolved": False, "reason": "unknown"}
            self._expire_if_due(q)
            if q.status != "pending":
                return {"resolved": False, "reason": q.status}
            try:
                validated = validate_reply(answer, q.reply_schema)
            except ReplyValidationError as exc:
                return {"resolved": False, "reason": str(exc)}
            q.reply = validated
            q.status = "answered"
            q.answered_at = self._clock()
            self._persist()
            self._checkpoint_question(q, "answered", reply=validated)
            event = self._events.get(question_id)
        if event is not None:
            event.set()
        self._emit(
            "human_gate.reply",
            {"id": question_id, "session": q.session_id, "status": "answered"},
            q.session_id,
        )
        logger.info("human question resolved id=%s session=%s", question_id, q.session_id)
        return {"resolved": True, "reason": "", "reply": validated}

    def list_questions(
        self,
        status: Optional[str] = None,
        requester: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[HumanQuestion]:
        """Generic list for the daemon's pending-question view. Expired
        pending questions flip to 'expired' lazily on list."""
        with self._lock:
            out = []
            for q in self._questions.values():
                self._expire_if_due(q)
                if status is not None and q.status != status:
                    continue
                if requester is not None and q.requester != requester:
                    continue
                if session_id is not None and q.session_id != session_id:
                    continue
                out.append(q)
            return list(out)

    # ---------- inspection ----------

    def get_question(self, question_id: str) -> Optional[HumanQuestion]:
        with self._lock:
            return self._questions.get(question_id)

    def pending(self, session_id: Optional[str] = None) -> List[HumanQuestion]:
        with self._lock:
            out = []
            for q in self._questions.values():
                self._expire_if_due(q)
                if q.status == "pending" and (session_id is None or q.session_id == session_id):
                    out.append(q)
            return out

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

    def _checkpoint_question(self, q: HumanQuestion, kind: str, reply=None) -> None:
        """Issue #125: mirror the question into the wired CheckpointStore
        under the `awaiting_human` marker (same seq reused per question so
        the entry is REPLACEd in place). Upgrade, not dependency: a
        checkpoint failure logs a warning and the JSON queue stays authoritative."""
        if self.checkpoint_ref is None:
            return
        try:
            seq = q.payload.get("checkpoint_seq") if isinstance(q.payload, dict) else None
            state = CheckpointState(
                messages=[],
                permission_mode=AWAITING_HUMAN_MARKER,
                metadata={
                    "awaiting_human": {
                        "question_id": q.id,
                        "session_id": q.session_id,
                        "question": q.question,
                        "payload": dict(q.payload or {}),
                        "reply_schema": dict(q.reply_schema or {}),
                        "policy": q.policy,
                        "requester": q.requester,
                        "expires_at": q.expires_at,
                        "checkpoint_kind": kind,
                        "reply": reply,
                    }
                },
                lineage_id=q.session_id,
            )
            saved = self.checkpoint_ref.save(q.session_id, state, seq=seq)
            if isinstance(q.payload, dict):
                q.payload["checkpoint_seq"] = saved
            logger.info(
                "awaiting_human checkpoint kind=%s id=%s session=%s seq=%s",
                kind,
                q.id,
                q.session_id,
                saved,
            )
        except Exception as exc:
            logger.warning("awaiting_human checkpoint failed id=%s: %s", q.id, exc)

    def _emit(self, event: str, payload: Dict[str, Any], session_id: str = "") -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit("human_gate", event, payload, session_id)
        except Exception as exc:
            logger.warning("human gate emit failed: %s", exc)


__all__ = [
    "AWAITING_HUMAN_MARKER",
    "HumanGate",
    "HumanGateError",
    "HumanQuestion",
    "ReplyValidationError",
    "validate_reply",
]

# Consumer contract (issue #66): the daemon exposes list/resolve over its
# RPC surface (list_questions / resolve / reply / cancel) — card UI and
# approval-routing policies stay consumer-side. Asking agents block on
# wait() (block policy) or poll take_reply() (continue policy). Questions
# carry requester + optional ttl_s expiry; expiry is a lazy status
# transition, expired questions reject resolution loudly-not-silently.
