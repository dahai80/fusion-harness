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
        node_id=str(data.get("node_id") or ""),
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
    def __init__(self, path: str) -> None:
        self.path = path

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
                    raise TrajectorySchemaError("line %d not JSON: %s" % (line_no, exc))
                normalized = normalize_record(data)
                if isinstance(normalized, list):
                    yield from normalized
                else:
                    yield normalized

    def read_all(self) -> List[TrajectoryRecord]:
        return list(self.stream())


__all__ = [
    "EVENTS",
    "SCHEMA_VERSION",
    "TrajectoryRecord",
    "TrajectorySchemaError",
    "TrajectoryWriter",
    "TrajectoryReader",
    "normalize_record",
    "register_legacy_mapping",
]
