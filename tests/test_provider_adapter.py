"""Tests for provider adapter contract K1 (#158)."""

import pytest

from fusion_harness import (
    EVENT_VARIANTS,
    TOOL_KINDS,
    AdapterEventError,
    AgentCapabilityFlags,
    AgentClient,
    AgentSession,
    AgentStreamEvent,
    ToolCallDetail,
    normalize_tool_kind,
)


def test_tool_kind_constants():
    assert "shell" in TOOL_KINDS
    assert "other" in TOOL_KINDS


def test_normalize_tool_kind_known_families():
    assert normalize_tool_kind("Bash") == "shell"
    assert normalize_tool_kind("run_terminal_cmd") == "shell"
    assert normalize_tool_kind("Read") == "read"
    assert normalize_tool_kind("str_replace_editor") == "edit"
    assert normalize_tool_kind("Write") == "write"
    assert normalize_tool_kind("Grep") == "search"
    assert normalize_tool_kind("web_fetch") == "web"
    assert normalize_tool_kind("mcp__x__y") == "mcp"


def test_normalize_tool_kind_unknown_is_other():
    assert normalize_tool_kind("quantum_flux") == "other"
    assert normalize_tool_kind("") == "other"


def test_tool_call_detail_auto_normalizes_kind():
    detail = ToolCallDetail(tool_name="Bash", args_summary="ls -la", call_id="c1")
    assert detail.kind == "shell"


def test_stream_event_validates_variant():
    event = AgentStreamEvent(variant="text_delta", payload={"text": "hi"})
    assert event.variant == "text_delta"
    with pytest.raises(AdapterEventError):
        AgentStreamEvent(variant="banana")


def test_event_variant_count_contract():
    assert len(EVENT_VARIANTS) == 18


def test_capability_flags_round_trip():
    flags = AgentCapabilityFlags(streaming=True, tool_use=True, context_compaction=True)
    restored = AgentCapabilityFlags.from_features(flags.to_features())
    assert restored == flags


def test_capability_flags_absent_means_false():
    flags = AgentCapabilityFlags.from_features(None)
    assert flags.streaming is False
    assert flags.context_compaction is False
    flags2 = AgentCapabilityFlags.from_features({"streaming": True})
    assert flags2.streaming is True
    assert flags2.tool_use is False


def test_capabilities_serializable_shape():
    features = AgentCapabilityFlags(tool_use=True).to_features()
    assert isinstance(features, dict)
    assert all(isinstance(v, bool) for v in features.values())


class _FakeSession:
    def session_handle(self) -> str:
        return "handle-123"

    async def send_prompt(self, prompt: str):
        yield AgentStreamEvent(variant="turn_started")
        yield AgentStreamEvent(variant="text_delta", payload={"text": prompt})

    async def interrupt(self) -> bool:
        return True


class _FakeClient:
    def __init__(self):
        self._flags = AgentCapabilityFlags(streaming=True, tool_use=True, session_resume=True)

    def capabilities(self) -> AgentCapabilityFlags:
        return self._flags

    async def new_session(self, session_id: str) -> _FakeSession:
        return _FakeSession()

    async def resume_session(self, session_id: str, handle: str) -> _FakeSession:
        return _FakeSession()


def test_fake_client_satisfies_protocols():
    client = _FakeClient()
    assert isinstance(client, AgentClient)
    assert isinstance(_FakeSession(), AgentSession)


@pytest.mark.asyncio
async def test_fake_session_streams_normalized_events():
    session = await _FakeClient().new_session("s1")
    events = [event async for event in session.send_prompt("hello")]
    assert [event.variant for event in events] == ["turn_started", "text_delta"]
    assert events[1].payload["text"] == "hello"
    assert await session.interrupt() is True
    assert session.session_handle() == "handle-123"
