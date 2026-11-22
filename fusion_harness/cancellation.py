"""Cancellation idempotency contract (issue #156, Paseo parity H2).

Paseo (v0.17.1, docs/agent-lifecycle.md:109-118) enforces three rules at
the session boundary; this module ports them to the harness kernel:

1. Interrupt is idempotent at the session boundary — cancelling the same
   token twice changes state once and reports the same outcome.
2. Cancel tokens carry turn identity — a stale token (its turn is no
   longer the session's current turn) is a no-op and can never settle a
   new turn.
3. "Accepting new work after an ambiguous interruption = split-brain" is
   an explicit invariant — after `mark_ambiguous`, `begin_turn` raises a
   typed error until the caller explicitly `rebind()`s.

Pure stdlib, deterministic, zero model calls. Lifecycle events are
emitted on the optional LifecycleBus (turn_cancelled / turn_ambiguous /
turn_rebind); the TURN_INTERRUPT / TURN_REBIND hook events (hooks.py)
are available for consumers owning an event loop.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

__all__ = [
    "CancelToken",
    "CancellationRegistry",
    "TurnStateError",
]


class TurnStateError(RuntimeError):
    """Illegal turn-state operation: split-brain guard tripped or stale
    settle attempt (fail visibly, never silently accept new work)."""


@dataclass
class CancelToken:
    """Carries turn identity: a token only ever settles the turn it was
    issued for."""

    session_id: str
    turn_id: str
    token_id: str


class CancellationRegistry:
    """Session-boundary cancellation state machine. Idempotent cancel,
    stale-token no-op, split-brain guard for ambiguous interruptions."""

    def __init__(self, bus: Any = None) -> None:
        self._bus = bus
        self._current: Dict[str, str] = {}
        self._cancelled: Set[str] = set()
        self._ambiguous: Set[str] = set()

    def begin_turn(self, session_id: str, turn_id: str) -> CancelToken:
        if session_id in self._ambiguous:
            raise TurnStateError(
                "split-brain guard: session %s has an ambiguous interruption — "
                "call rebind() explicitly before new work" % session_id
            )
        self._current[session_id] = turn_id
        logger.info("turn begun session=%s turn=%s", session_id, turn_id)
        return CancelToken(session_id=session_id, turn_id=turn_id, token_id=self._new_token_id())

    def cancel(self, token: CancelToken) -> bool:
        """Idempotent cancel. True = this token settled its turn (first
        call) or already settled it (repeat); False = stale token, a
        no-op that never touches the session's current turn."""
        if token.token_id in self._cancelled:
            logger.info("cancel idempotent repeat token=%s", token.token_id)
            return True
        if self._current.get(token.session_id) != token.turn_id:
            logger.info(
                "stale cancel token ignored session=%s turn=%s current=%s",
                token.session_id,
                token.turn_id,
                self._current.get(token.session_id),
            )
            return False
        self._cancelled.add(token.token_id)
        self._emit(
            "turn_cancelled",
            {"session_id": token.session_id, "turn_id": token.turn_id, "token_id": token.token_id},
        )
        logger.info("turn cancelled session=%s turn=%s", token.session_id, token.turn_id)
        return True

    def mark_ambiguous(self, session_id: str, turn_id: str) -> bool:
        """Flag an interruption whose outcome is unknown (e.g. cancel sent,
        acknowledgment never received). Blocks new work until rebind()."""
        if self._current.get(session_id) != turn_id:
            logger.info("ambiguous mark ignored for non-current turn session=%s", session_id)
            return False
        self._ambiguous.add(session_id)
        self._emit("turn_ambiguous", {"session_id": session_id, "turn_id": turn_id})
        logger.warning(
            "ambiguous interruption session=%s turn=%s — new work blocked until rebind",
            session_id,
            turn_id,
        )
        return True

    def rebind(self, session_id: str, turn_id: str) -> CancelToken:
        """Explicit re-binding after an ambiguous interruption — the only
        path back to accepting new work (no silent split-brain)."""
        self._ambiguous.discard(session_id)
        self._current[session_id] = turn_id
        self._emit("turn_rebind", {"session_id": session_id, "turn_id": turn_id})
        logger.info("turn rebound session=%s turn=%s", session_id, turn_id)
        return CancelToken(session_id=session_id, turn_id=turn_id, token_id=self._new_token_id())

    def is_ambiguous(self, session_id: str) -> bool:
        return session_id in self._ambiguous

    def current_turn(self, session_id: str) -> str:
        return self._current.get(session_id, "")

    @staticmethod
    def _new_token_id() -> str:
        return "cancel-" + uuid.uuid4().hex[:12]

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit("cancellation", event, payload, session_id="")
        except Exception as exc:
            logger.warning("cancellation lifecycle emit failed event=%s: %s", event, exc)
