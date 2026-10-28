# Issue #4: durable append-only session event log + replay API.
"""Durable session events: append-only, schema-versioned, replayable.

Domain separation is structural (issue #4 AC2): the durable type is
SessionEvent; the live in-process type (funnels.LifecycleEvent) is an
unrelated class, and SessionEventLog.append_event() rejects anything that
is not a SessionEvent with a TypeError — live hook events cannot reach the
log by convention, oversight, or coercion.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Durable event types the replay reducer understands. Unknown types are
# preserved on replay (logged) but do not change reconstructed state.
_DURABLE_EVENT_TYPES = (
    "session_start",
    "session_end",
    "message",
    "tool_call",
    "tool_result",
    "tool_error",
    "compaction",
)


class LogSchemaError(Exception):
    pass


@dataclass(frozen=True)
class SessionEvent:
    # Immutable durable record — an appended event can never be edited.
    seq: int
    ts: float
    type: str
    payload: dict
    session_id: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SessionEvent:
        try:
            return cls(
                seq=int(data["seq"]),
                ts=float(data["ts"]),
                type=str(data["type"]),
                payload=dict(data.get("payload") or {}),
                session_id=str(data.get("session_id", "")),
                schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LogSchemaError(f"malformed session event: {exc}") from exc


@dataclass
class SessionState:
    # Result of replaying a log: reconstructed session state.
    session_id: str = ""
    messages: list[dict] = field(default_factory=list)
    completed_tool_calls: list[dict] = field(default_factory=list)
    # Crash mid-tool-call: a tool_call with no tool_result/tool_error after
    # it. Recorded explicitly (dsh semantics) — never silently dropped.
    incomplete_tool_calls: list[dict] = field(default_factory=list)
    compactions: int = 0
    event_count: int = 0
    ended: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "completed_tool_calls": self.completed_tool_calls,
            "incomplete_tool_calls": self.incomplete_tool_calls,
            "compactions": self.compactions,
            "event_count": self.event_count,
            "ended": self.ended,
        }


def _state_reducer(state: SessionState, event: SessionEvent) -> None:
    etype = event.type
    if etype == "message":
        state.messages.append(dict(event.payload))
    elif etype == "tool_call":
        record = {"call_id": event.payload.get("call_id", f"seq-{event.seq}"), "event": event.to_dict()}
        state.incomplete_tool_calls.append(record)
    elif etype in ("tool_result", "tool_error"):
        call_id = event.payload.get("call_id", "")
        remaining = []
        for pending in state.incomplete_tool_calls:
            if call_id and pending["call_id"] == call_id:
                state.completed_tool_calls.append(
                    {
                        "call_id": call_id,
                        "call": pending["event"]["payload"],
                        "outcome": dict(event.payload),
                        "ok": etype == "tool_result",
                    }
                )
            else:
                remaining.append(pending)
        state.incomplete_tool_calls = remaining
    elif etype == "compaction":
        state.compactions += 1
    elif etype == "session_end":
        state.ended = True
    else:
        logger.debug("replay: preserved unknown event type=%s seq=%s", etype, event.seq)


class SessionEventLog:
    """Append-only durable log for one session, optional JSONL persistence.

    Appends never rewrite history: events get monotonically increasing seq
    numbers, and the JSONL file is opened in append mode only.
    """

    def __init__(self, session_id: str, log_dir: str | None = None):
        self.session_id = session_id
        self._events: list[SessionEvent] = []
        self._next_seq = 1
        self._fh = None
        self._path: Path | None = None
        if log_dir:
            self._path = Path(log_dir) / f"{session_id}.jsonl"
            self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- append ------------------------------------------------------------

    def append(self, event_type: str, payload: dict | None = None) -> SessionEvent:
        event = SessionEvent(
            seq=self._next_seq,
            ts=time.time(),
            type=event_type,
            payload=dict(payload or {}),
            session_id=self.session_id,
        )
        self.append_event(event)
        return event

    def append_event(self, event: SessionEvent) -> None:
        # Structural domain separation (#4 AC2): only SessionEvent is
        # durable. Live events (funnels.LifecycleEvent) fail here loudly.
        if not isinstance(event, SessionEvent):
            raise TypeError(
                f"only SessionEvent is appendable to the durable log, got {type(event).__name__} "
                "(live lifecycle events are in-process only)"
            )
        if event.seq != self._next_seq:
            raise LogSchemaError(
                f"non-monotonic seq {event.seq}, expected {self._next_seq} — append-only violated"
            )
        if event.type not in _DURABLE_EVENT_TYPES:
            logger.warning("durable log: unknown event type=%s (preserved, not reduced)", event.type)
        self._events.append(event)
        self._next_seq += 1
        if self._path is not None:
            self._persist(event)

    def _persist(self, event: SessionEvent) -> None:
        try:
            if self._fh is None:
                self._fh = self._path.open("a", encoding="utf-8")
            self._fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            logger.error("session log persist failed session=%s seq=%s: %s", self.session_id, event.seq, exc)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    # -- read / replay -----------------------------------------------------

    def events(self) -> list[SessionEvent]:
        return list(self._events)

    def replay(self) -> SessionState:
        state = SessionState(session_id=self.session_id, event_count=len(self._events))
        for event in self._events:
            _state_reducer(state, event)
        if state.incomplete_tool_calls:
            logger.warning(
                "replay session=%s: %d tool call(s) without result — recorded as incomplete",
                self.session_id,
                len(state.incomplete_tool_calls),
            )
        return state

    @classmethod
    def load(cls, path: str | Path) -> SessionEventLog:
        # Post-restart reconstruction (#4 AC1): read the JSONL back, validate
        # every record's schema version, resume seq numbering where it left off.
        target = Path(path)
        session_id = target.stem
        log = cls(session_id)
        if not target.exists():
            logger.warning("session log not found: %s", target)
            return log
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.error("session log read failed %s: %s", target, exc)
            return log
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LogSchemaError(f"corrupt line in {target}: {exc}") from exc
            if int(data.get("schema_version", 0)) > SCHEMA_VERSION:
                raise LogSchemaError(
                    f"log {target} has schema_version {data.get('schema_version')} > "
                    f"supported {SCHEMA_VERSION} — upgrade fusion-harness first"
                )
            event = SessionEvent.from_dict(data)
            event = SessionEvent(
                seq=log._next_seq,
                ts=event.ts,
                type=event.type,
                payload=event.payload,
                session_id=event.session_id or session_id,
                schema_version=event.schema_version,
            )
            log._events.append(event)
            log._next_seq += 1
        logger.info("session log loaded path=%s events=%d", target, len(log._events))
        return log


__all__ = [
    "LogSchemaError",
    "SCHEMA_VERSION",
    "SessionEvent",
    "SessionEventLog",
    "SessionState",
]
