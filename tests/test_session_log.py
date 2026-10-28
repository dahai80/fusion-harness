"""Session event log coverage: issue #4 (durable vs live domains)."""

import json

import pytest

from fusion_harness.funnels import EventDomain, LifecycleBus, LifecycleEvent
from fusion_harness.session_log import (
    SCHEMA_VERSION,
    LogSchemaError,
    SessionEvent,
    SessionEventLog,
)


def _log(tmp_path, session_id="s1", persist=True):
    return SessionEventLog(session_id, log_dir=str(tmp_path) if persist else None)


def test_append_only_monotonic_seq():
    log = _log(None, persist=False)
    e1 = log.append("session_start", {"agent": "n0"})
    e2 = log.append("message", {"role": "user", "content": "hi"})
    assert e1.seq == 1 and e2.seq == 2
    with pytest.raises(LogSchemaError):
        log.append_event(SessionEvent(seq=1, ts=0.0, type="message", payload={}, session_id="s1"))


def test_events_are_immutable_records():
    log = _log(None, persist=False)
    log.append("message", {"role": "user", "content": "hi"})
    with pytest.raises(Exception):
        log.events()[0].payload = {"role": "user", "content": "tampered"}


def test_live_lifecycle_event_rejected_structurally():
    # AC2: domain separation is structural — a LifecycleEvent (live domain)
    # cannot be appended to the durable log, regardless of intent.
    bus = LifecycleBus()
    live = bus.emit(EventDomain.AGENT, "agent.tick", {"n": 1}, session_id="s1")
    assert isinstance(live, LifecycleEvent)
    log = _log(None, persist=False)
    with pytest.raises(TypeError, match="live lifecycle events"):
        log.append_event(live)


def test_replay_after_restart_reconstructs_state(tmp_path):
    # AC1: durable log round-trips through the JSONL file; a fresh process
    # loads it and replay rebuilds identical state.
    log = _log(tmp_path)
    log.append("session_start", {"agent": "n0"})
    log.append("message", {"role": "user", "content": "hi"})
    log.append("tool_call", {"call_id": "c1", "tool": "read_file", "args": {"path": "/tmp/a"}})
    log.append("tool_result", {"call_id": "c1", "ok": True, "preview": "data"})
    log.append("message", {"role": "assistant", "content": "done"})
    state_before = log.replay()
    log.close()

    restored = SessionEventLog.load(tmp_path / "s1.jsonl")
    state_after = restored.replay()
    assert state_after.to_dict() == state_before.to_dict()
    assert len(state_after.messages) == 2
    assert len(state_after.completed_tool_calls) == 1
    assert state_after.completed_tool_calls[0]["call_id"] == "c1"
    # append-only survives restart: seq continues after the restored tail
    e = restored.append("session_end", {})
    assert e.seq == 6


def test_crash_mid_tool_call_records_missing_result_explicitly(tmp_path):
    # AC3: process dies between tool_call and tool_result — replay yields
    # consistent state with the missing result recorded, never dropped.
    log = _log(tmp_path)
    log.append("session_start", {"agent": "n0"})
    log.append("tool_call", {"call_id": "c9", "tool": "shell", "args": {"command": "sleep 5"}})
    log.close()  # crash: no tool_result was ever appended

    restored = SessionEventLog.load(tmp_path / "s1.jsonl")
    state = restored.replay()
    assert state.completed_tool_calls == []
    assert len(state.incomplete_tool_calls) == 1
    assert state.incomplete_tool_calls[0]["call_id"] == "c9"
    assert state.incomplete_tool_calls[0]["event"]["payload"]["tool"] == "shell"


def test_schema_version_in_every_record(tmp_path):
    log = _log(tmp_path)
    log.append("session_start", {})
    log.close()
    rows = [json.loads(line) for line in (tmp_path / "s1.jsonl").read_text().splitlines()]
    assert rows and all(r["schema_version"] == SCHEMA_VERSION for r in rows)


def test_load_rejects_newer_schema(tmp_path):
    target = tmp_path / "s2.jsonl"
    target.write_text(
        json.dumps({"seq": 1, "ts": 0.0, "type": "session_start", "payload": {},
                    "session_id": "s2", "schema_version": SCHEMA_VERSION + 1}) + "\n"
    )
    with pytest.raises(LogSchemaError, match="schema_version"):
        SessionEventLog.load(target)


def test_load_rejects_corrupt_line(tmp_path):
    target = tmp_path / "s3.jsonl"
    target.write_text("{not json}\n")
    with pytest.raises(LogSchemaError, match="corrupt"):
        SessionEventLog.load(target)


def test_replay_preserves_unknown_event_type():
    log = _log(None, persist=False)
    log.append("session_start", {})
    log.append("custom_future_event", {"x": 1})  # forward-compat: kept, not reduced
    state = log.replay()
    assert state.event_count == 2
    assert state.messages == []
