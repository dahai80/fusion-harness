"""Provider seam tests (#15): event model, adapter host, wire codec, subprocess runtime."""

import ast
import os
import sys

import pytest

from fusion_harness.provider import (  # noqa: E402
    AdapterHost,
    EventBus,
    MESSAGE_CHUNK,
    PERMISSION_REQUEST,
    PERMISSION_RESPONSE,
    SESSION_NEW,
    SessionEvent,
    SubprocessSessionRuntime,
    wire,
)

DRIVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "provider_driver.py")


def driver_command():
    return [sys.executable, DRIVER_PATH]


class TestEventModel:
    def test_event_roundtrip(self):
        event = SessionEvent(
            type=MESSAGE_CHUNK, session_id="s1", payload={"text": "hi"}, request_id="r1"
        )
        data = event.to_dict()
        restored = SessionEvent.from_dict(data)
        assert restored.type == MESSAGE_CHUNK
        assert restored.payload == {"text": "hi"}
        assert restored.request_id == "r1"

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError):
            SessionEvent(type="session/bogus", session_id="s1")

    def test_bus_fanout_and_error_isolation(self):
        bus = EventBus()
        seen = []
        bus.subscribe(lambda e: seen.append(e))

        def _boom(_event):
            raise RuntimeError("subscriber crash")

        bus.subscribe(_boom)
        bus.publish(SessionEvent(type=SESSION_NEW, session_id="s1"))
        assert len(seen) == 1


class TestWireCodec:
    def test_request_response_notification_roundtrip(self):
        kind, msg = wire.decode_line(wire.encode_request(1, "initialize", {"a": 1}))
        assert kind == "request" and msg["method"] == "initialize"
        kind, msg = wire.decode_line(wire.encode_response(1, {"ok": True}))
        assert kind == "response" and msg["result"] == {"ok": True}
        kind, msg = wire.decode_line(wire.encode_notification("session/update", {}))
        assert kind == "notification" and "id" not in msg

    def test_parse_error(self):
        with pytest.raises(wire.WireError) as exc:
            wire.decode_line("not json")
        assert exc.value.code == wire.PARSE_ERROR

    def test_error_encoding(self):
        kind, msg = wire.decode_line(wire.encode_error(3, -32601, "nope"))
        assert kind == "error" and msg["error"]["code"] == -32601


class TestAdapterHost:
    @pytest.mark.asyncio
    async def test_permission_fail_closed_without_handler(self):
        host = AdapterHost()
        decision = await host.request_permission("s1", {"tool": "bash"})
        assert decision["allow"] is False
        assert decision["reason"] == "no permission handler"

    @pytest.mark.asyncio
    async def test_permission_handler_roundtrip_emits_events(self):
        seen = []

        async def handler(payload):
            seen.append(payload)
            return {"allow": True}

        host = AdapterHost(permission_handler=handler)
        decision = await host.request_permission("s1", {"tool": "bash"})
        assert decision["allow"] is True
        assert seen[0]["session_id"] == "s1"
        assert seen[0]["request_id"]

    @pytest.mark.asyncio
    async def test_permission_handler_crash_denies(self):
        def handler(_payload):
            raise RuntimeError("boom")

        host = AdapterHost(permission_handler=handler)
        decision = await host.request_permission("s1", {})
        assert decision["allow"] is False

    def test_duplicate_registration_rejected(self):
        host = AdapterHost()
        adapter = object.__new__(AdapterHost)
        adapter.name = "x"
        host.register(adapter)
        with pytest.raises(ValueError):
            host.register(adapter)


class TestSubprocessRuntime:
    @pytest.mark.asyncio
    async def test_end_to_end_prompt_streaming(self):
        host = AdapterHost()
        runtime = SubprocessSessionRuntime("echo", driver_command(), host, startup_timeout=15.0)
        events = []
        host.bus.subscribe(lambda e: events.append(e))
        await runtime.start()
        assert runtime.state.value == "ready"
        await runtime.new_session("sess-1", cwd=os.getcwd())
        reply = await runtime.prompt("sess-1", "hello world")
        assert reply["ok"] is True
        chunks = [e for e in events if e.type == MESSAGE_CHUNK]
        assert "".join(e.payload["text"] for e in chunks) == "hello world"
        await runtime.stop()
        assert runtime.state.value == "closed"

    @pytest.mark.asyncio
    async def test_permission_roundtrip_via_host(self):
        async def handler(payload):
            return {"allow": payload.get("tool") == "bash"}

        host = AdapterHost(permission_handler=handler)
        runtime = SubprocessSessionRuntime("echo", driver_command(), host, startup_timeout=15.0)
        events = []
        host.bus.subscribe(lambda e: events.append(e))
        await runtime.start()
        await runtime.new_session("sess-2")
        reply = await runtime.prompt("sess-2", "!perm run tests")
        assert reply["result"]["permission"] == "granted"
        request_events = [e for e in events if e.type == PERMISSION_REQUEST]
        response_events = [e for e in events if e.type == PERMISSION_RESPONSE]
        assert len(request_events) == 1 and request_events[0].payload["tool"] == "bash"
        assert response_events[0].payload["allow"] is True
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_permission_denied_flows_to_driver(self):
        host = AdapterHost(permission_handler=lambda _p: {"allow": False})
        runtime = SubprocessSessionRuntime("echo", driver_command(), host, startup_timeout=15.0)
        await runtime.start()
        await runtime.new_session("sess-3")
        reply = await runtime.prompt("sess-3", "!perm danger")
        assert reply["result"]["permission"] == "denied"
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_prompt_before_start_raises(self):
        runtime = SubprocessSessionRuntime("echo", driver_command(), AdapterHost())
        with pytest.raises(Exception):
            await runtime.prompt("s", "hi")


class TestStdlibOnly:
    def test_provider_modules_import_only_stdlib(self):
        provider_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fusion_harness",
            "provider",
        )
        stdlib_top = set(sys.stdlib_module_names)
        for fname in os.listdir(provider_dir):
            if not fname.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(provider_dir, fname)).read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for mod in mods:
                    root = mod.split(".")[0]
                    assert root in stdlib_top or root == "fusion_harness", (
                        "%s imports non-stdlib %s" % (fname, mod)
                    )
