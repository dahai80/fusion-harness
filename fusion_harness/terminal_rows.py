"""Terminal-row invariant for lifecycle hooks (issue #157, Paseo parity H9).

Paseo (v0.17.1, codex-app-server-agent.ts:7144) terminalizes every
transient timeline row before emitting a turn-termination event —
otherwise the persisted timeline ends with dangling in-flight rows after
an abnormal turn end. This module ports that invariant to the harness
kernel as a deterministic tracker plus a validation hook:

- `TerminalRowTracker` records open transient rows (tool_call, llm_call,
  subagent_run, ...) per session and force-closes/annotates any rows
  still open when a turn reaches a terminal state
  (completed/failed/cancelled).
- `wire_terminal_invariant` registers callback hooks on STOP / SESSION_END
  / ERROR so the invariant runs through the normal hook chain.

Pure stdlib, deterministic, zero model calls. Force-close emits
`terminal_row_violation` on the optional LifecycleBus and never gates.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = ("STOP", "SESSION_END", "ERROR")

__all__ = [
    "TERMINAL_EVENTS",
    "TerminalRowTracker",
    "wire_terminal_invariant",
]


class TerminalRowTracker:
    """Open transient rows per session + terminal-state force-close."""

    def __init__(self, bus: Any = None) -> None:
        self._bus = bus
        self._open: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def open_row(
        self,
        session_id: str,
        row_id: str,
        kind: str = "tool_call",
        turn_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        session_rows = self._open.setdefault(session_id, {})
        if row_id in session_rows:
            logger.warning("duplicate open_row ignored session=%s row=%s", session_id, row_id)
            return False
        session_rows[row_id] = {
            "row_id": row_id,
            "kind": kind,
            "turn_id": turn_id,
            "metadata": dict(metadata or {}),
        }
        logger.info("row opened session=%s row=%s kind=%s", session_id, row_id, kind)
        return True

    def close_row(self, session_id: str, row_id: str, outcome: str = "done") -> bool:
        row = self._open.get(session_id, {}).pop(row_id, None)
        if row is None:
            logger.info("close_row ignored unknown row session=%s row=%s", session_id, row_id)
            return False
        row["outcome"] = outcome
        logger.info("row closed session=%s row=%s outcome=%s", session_id, row_id, outcome)
        return True

    def open_rows(self, session_id: str) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._open.get(session_id, {}).values()]

    def terminalize_turn(
        self,
        session_id: str,
        turn_id: str = "",
        outcome: str = "completed",
    ) -> List[Dict[str, Any]]:
        """Invariant check at turn end: force-close every transient row
        still open (scoped to turn_id when given, else the whole session),
        annotated `forced=True` so the persisted timeline never dangles.
        Returns the force-closed rows."""
        session_rows = self._open.get(session_id, {})
        targets = [
            row_id
            for row_id, row in session_rows.items()
            if not turn_id or row.get("turn_id") == turn_id
        ]
        forced = []
        for row_id in targets:
            row = session_rows.pop(row_id)
            row["forced"] = True
            row["forced_outcome"] = outcome
            forced.append(row)
            logger.warning(
                "terminal-row violation: force-closed row session=%s row=%s kind=%s outcome=%s",
                session_id,
                row_id,
                row.get("kind"),
                outcome,
            )
        if forced:
            self._emit(
                "terminal_row_violation",
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "outcome": outcome,
                    "forced_rows": [row["row_id"] for row in forced],
                },
            )
        return forced

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(
                "lifecycle", event, payload, session_id=str(payload.get("session_id", ""))
            )
        except Exception as exc:
            logger.warning("terminal-row lifecycle emit failed event=%s: %s", event, exc)


def wire_terminal_invariant(hook_engine: Any, tracker: TerminalRowTracker) -> None:
    """Register callback hooks on STOP / SESSION_END / ERROR so the
    invariant runs through the normal hook chain (callback returns None:
    observational, never gates)."""
    from fusion_harness.hooks import HookConfig

    def _make_callback():
        def callback(payload: Dict[str, Any]):
            try:
                tracker.terminalize_turn(
                    session_id=str(payload.get("session_id", "")),
                    turn_id=str(payload.get("turn_id", "")),
                    outcome=str(payload.get("outcome", "completed")),
                )
            except Exception as exc:
                logger.warning("terminal invariant hook error: %s", exc)
            return None

        return callback

    for event in TERMINAL_EVENTS:
        hook_engine.register(HookConfig(event=event, type="callback", callback=_make_callback()))
        logger.info("terminal-row invariant hook registered event=%s", event)
