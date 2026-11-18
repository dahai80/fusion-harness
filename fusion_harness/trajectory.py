"""Unified trajectory schema (issue #36, K3).

One normative JSONL record shape for DAG and team runs. Writer emits ONLY
the normative schema; reader accepts legacy cowork- and studio-shaped
records via a normalizing adapter so migration is gradual.

Privacy rule: model_call_meta carries token counts and the model NAME only
— never prompts or completions. Zero model calls.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

EVENTS = ("start", "tool_call", "tool_result", "model_call_meta", "error", "end")
SCHEMA_VERSION = 1


class TrajectorySchemaError(ValueError):
    """Record violates the normative trajectory schema."""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class TrajectoryRecord:
    run_id: str
    node_id: str = ""
    member: str = ""
    ts: int = 0
    event: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    status: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise TrajectorySchemaError("run_id required")
        if not self.node_id and not self.member:
            raise TrajectorySchemaError("node_id or member required (DAG and team runs both)")
        if self.event not in EVENTS:
            raise TrajectorySchemaError(
                "unknown event: %s (allowed: %s)" % (self.event, "|".join(EVENTS))
            )
        self.ts = self.ts or _now_ms()
        if self.event == "model_call_meta":
            banned = ("prompt", "completion", "messages", "content", "text")
            leaked = [k for k in self.payload if k.lower() in banned]
            if leaked:
                raise TrajectorySchemaError(
                    "model_call_meta must not carry prompt/completion content: %s" % leaked
                )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def _norm(
    run_id: str = "",
    node_id: str = "",
    member: str = "",
    ts: int = 0,
    event: str = "",
    payload: Optional[Dict[str, Any]] = None,
    duration_ms: int = 0,
    status: str = "",
    **legacy: Any,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        run_id=run_id,
        node_id=node_id,
        member=member,
        ts=ts,
        event=event,
        payload=payload or {},
        duration_ms=duration_ms,
        status=status,
    )


_LEGACY_MAPPINGS: List[Any] = []

# legacy event name -> nearest normative event (original kept in payload)
_LEGACY_EVENT_MAP = {
    "workflow_start": "start",
    "session_start": "start",
    "agent_start": "start",
    "workflow_end": "end",
    "session_end": "end",
    "agent_stop": "end",
    "workflow_cancel": "end",
    "node_error": "error",
    "pre_node_execute": "start",
    "post_node_execute": "tool_result",
    "tool_call_start": "tool_call",
    "think": "model_call_meta",
    "permission_request": "tool_call",
    "config_change": "tool_result",
    "notification": "tool_result",
    "pre_compact": "tool_result",
    "directive": "tool_call",
}


def _map_legacy_event(raw: str, payload: Dict[str, Any]) -> str:
    if raw in EVENTS:
        return raw
    payload["legacy_event"] = raw
    return _LEGACY_EVENT_MAP.get(raw, "tool_result")


def register_legacy_mapping(match: Any, mapping: Any) -> None:
    """Register an adapter for a legacy record shape (cowork/studio)."""
    _LEGACY_MAPPINGS.append((match, mapping))


def _adapt_cowork(data: Dict[str, Any]) -> Optional[TrajectoryRecord]:
    """cowork trajectory/recorder.py line shape: ts/execution_id/event/
    node_id/status/error/is_error/data."""
    if "ts" not in data or "execution_id" not in data:
        return None
    payload = dict(data.get("data") or {})
    error = data.get("error") or ""
    if error:
        payload["error"] = error
    raw_event = str(data.get("event") or "")
    status = str(data.get("status") or "")
    return _norm(
        run_id=str(
            data.get("execution_id") or data.get("workflow_id") or data.get("trace_id") or ""
        ),
        # Issue #81: legacy cowork workflow-level events carry an empty
        # node_id — fall back to a non-empty node scope instead of failing
        # TrajectoryRecord validation mid-stream.
        node_id=str(data.get("node_id") or data.get("member") or "workflow"),
        member=str(data.get("member") or ""),
        ts=int(float(data.get("ts") or 0) * 1000)
        if float(data.get("ts") or 0) < 10**12
        else int(data.get("ts") or 0),
        event=_map_legacy_event(raw_event, payload),
        payload=payload,
        status="error" if data.get("is_error") else status,
    )


def _adapt_studio(data: Dict[str, Any]) -> Optional[List[TrajectoryRecord]]:
    """studio trajectory_writer.py run shape: one JSON object per run with a
    top-level events[] list — flattened by the reader into per-event lines."""
    if "events" not in data or not isinstance(data.get("events"), list):
        return None
    run_id = str(data.get("trace_id") or data.get("session_id") or "")
    token_usage = data.get("token_usage") or {}
    out: List[TrajectoryRecord] = []
    for entry in data["events"]:
        if not isinstance(entry, dict):
            continue
        payload = {k: v for k, v in entry.items() if k not in ("type", "timestamp")}
        if token_usage:
            payload["run_token_usage"] = token_usage
        raw_event = str(entry.get("type") or "")
        status = "error" if raw_event == "error" else ""
        out.append(
            _norm(
                run_id=run_id,
                node_id=str(
                    entry.get("node_id")
                    or data.get("graph_id")
                    or data.get("session_id")
                    or run_id
                    or "run"
                ),
                member=str(data.get("agent_id") or ""),
                ts=int(entry.get("timestamp") or 0),
                event=_map_legacy_event(raw_event, payload),
                payload=payload,
                status=status,
            )
        )
    return out or None


register_legacy_mapping(lambda d: isinstance(d.get("events"), list), _adapt_studio)
register_legacy_mapping(lambda d: "ts" in d, _adapt_cowork)


def _adapt_legacy(data: Dict[str, Any]) -> Optional[TrajectoryRecord]:
    for match, mapping in _LEGACY_MAPPINGS:
        if match(data):
            return mapping(data)
    return None


def normalize_record(data: Dict[str, Any]) -> Any:
    """Reader-side adapter: known legacy shapes (cowork recorder / studio
    trajectory_writer) map first — a studio run record flattens into a LIST
    of records; normative shape passes through; unknown shapes are rejected
    loudly."""
    if not isinstance(data, dict):
        raise TrajectorySchemaError("trajectory record must be a JSON object")
    adapted = _adapt_legacy(data)
    if adapted is not None:
        return adapted
    if "run_id" in data and "event" in data:
        try:
            return _norm(**{k: v for k, v in data.items() if k in _norm.__code__.co_varnames})
        except (TypeError, TrajectorySchemaError):
            pass
    raise TrajectorySchemaError("unrecognized trajectory record shape: keys=%s" % sorted(data))


class TrajectoryWriter:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: TrajectoryRecord) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(record.to_json() + "\n")

    def write_event(
        self,
        run_id: str,
        event: str,
        node_id: str = "",
        member: str = "",
        payload: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
        status: str = "",
    ) -> TrajectoryRecord:
        record = TrajectoryRecord(
            run_id=run_id,
            node_id=node_id,
            member=member,
            event=event,
            payload=payload or {},
            duration_ms=duration_ms,
            status=status,
        )
        self.write(record)
        return record


class TrajectoryReader:
    def __init__(self, path: str, skip_corrupt: bool = False) -> None:
        self.path = path
        # Issue #81: best-effort mode for mixed pools — a single corrupt or
        # unknown-shape line is skipped with a warning instead of aborting
        # the whole file. Default stays strict (fail loudly).
        self.skip_corrupt = skip_corrupt
        self.skipped = 0

    def stream(self) -> Iterator[TrajectoryRecord]:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError as exc:
                    if self.skip_corrupt:
                        self.skipped += 1
                        logger.warning("trajectory line %d not JSON, skipped: %s", line_no, exc)
                        continue
                    raise TrajectorySchemaError("line %d not JSON: %s" % (line_no, exc))
                try:
                    normalized = normalize_record(data)
                except TrajectorySchemaError as exc:
                    if self.skip_corrupt:
                        self.skipped += 1
                        logger.warning(
                            "trajectory line %d failed normalization, skipped: %s", line_no, exc
                        )
                        continue
                    raise
                if isinstance(normalized, list):
                    yield from normalized
                else:
                    yield normalized

    def read_all(self) -> List[TrajectoryRecord]:
        return list(self.stream())


# Issue #55 (F13): discardable-span annotation. Observation only — nothing
# is dropped anywhere; fusion-mlx consumes the annotation once its engine
# exposes the KV drop primitive (#1645).
DISCARD_REASONS = ("stale_tool_output", "superseded_turn")


def mark_discardable(record: TrajectoryRecord, reason: str) -> TrajectoryRecord:
    """Return a copy of the record annotated as discardable with a reason.
    Unknown reasons are rejected — the annotation is a contract, not a note."""
    if reason not in DISCARD_REASONS:
        raise TrajectorySchemaError(
            "unknown discard reason: %s (allowed: %s)" % (reason, "|".join(DISCARD_REASONS))
        )
    payload = dict(record.payload)
    payload["discardable"] = reason
    return TrajectoryRecord(
        run_id=record.run_id,
        node_id=record.node_id,
        member=record.member,
        ts=record.ts,
        event=record.event,
        payload=payload,
        duration_ms=record.duration_ms,
        status=record.status,
    )


class PrefixStabilityTracker:
    """Per-agent system/prefix stability ratio (issue #55, F14): how stable
    the prompt head (system + first user turn) is across turns. harness
    only reports — the fusion-mlx warm-list consumes the metric (its own
    hot-prefix concentration threshold, >= 30%)."""

    def __init__(self) -> None:
        self._turns: Dict[str, int] = {}
        self._stable: Dict[str, int] = {}
        self._prefix: Dict[str, str] = {}

    @staticmethod
    def _prefix_key(system: str, first_user: str) -> str:
        import hashlib

        head = "%s\x00%s" % (system or "", first_user or "")
        return hashlib.sha1(head.encode("utf-8")).hexdigest()

    def observe(self, agent_id: str, system: str, first_user: str) -> None:
        key = self._prefix_key(system, first_user)
        self._turns[agent_id] = self._turns.get(agent_id, 0) + 1
        prev = self._prefix.get(agent_id)
        if prev is not None:
            # First turn establishes the baseline; only later turns compare.
            if prev == key:
                self._stable[agent_id] = self._stable.get(agent_id, 0) + 1
        self._prefix[agent_id] = key

    def observe_messages(self, agent_id: str, messages: list) -> None:
        system = ""
        first_user = ""
        for m in messages or []:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system" and not system:
                system = content
            elif role == "user" and not first_user:
                first_user = content
            if system and first_user:
                break
        self.observe(agent_id, system, first_user)

    def stability(self, agent_id: str) -> float:
        turns = self._turns.get(agent_id, 0)
        if turns <= 1:
            return 0.0
        return self._stable.get(agent_id, 0) / (turns - 1)

    def report(self) -> Dict[str, Dict[str, float]]:
        """Per-agent metric: {"agent": {"stability": ratio, "turns": n}}."""
        return {
            agent: {"stability": round(self.stability(agent), 4), "turns": self._turns[agent]}
            for agent in sorted(self._turns)
        }


__all__ = [
    "EVENTS",
    "SCHEMA_VERSION",
    "TrajectoryRecord",
    "TrajectorySchemaError",
    "TrajectoryWriter",
    "TrajectoryReader",
    "normalize_record",
    "register_legacy_mapping",
    "DISCARD_REASONS",
    "mark_discardable",
    "PrefixStabilityTracker",
]
