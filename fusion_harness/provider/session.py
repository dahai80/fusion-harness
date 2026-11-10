"""SubprocessSessionRuntime — drives one agent-runtime adapter over the wire (#15).

Lifecycle: spawn driver subprocess -> initialize handshake -> sessions created
per request -> prompts stream back as SessionEvents. Permission requests from
the driver round-trip through AdapterHost (HiTL); the runtime adds no policy
of its own — funnels/permission engine apply upstream, unchanged.
"""

import asyncio
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from fusion_harness.provider import wire
from fusion_harness.provider.adapter import AdapterHost
from fusion_harness.provider.events import (
    MESSAGE_CHUNK,
    SESSION_NEW,
    STATE_CHANGED,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
    SessionEvent,
)

logger = logging.getLogger(__name__)

_UPDATE_EVENT_MAP = {
    "message_chunk": MESSAGE_CHUNK,
    "tool_call": TOOL_CALL,
    "tool_result": TOOL_RESULT,
}


class SessionState(str, Enum):
    NEW = "new"
    READY = "ready"
    RUNNING = "running"
    AWAITING_PERMISSION = "awaiting_permission"
    CLOSED = "closed"


class RuntimeNotReady(Exception):
    pass


class SubprocessSessionRuntime:
    def __init__(
        self,
        name: str,
        command: List[str],
        host: AdapterHost,
        bus: Optional[EventBus] = None,
        cwd: Optional[str] = None,
        startup_timeout: float = 10.0,
    ) -> None:
        self.name = name
        self.command = list(command)
        self.host = host
        self.bus = bus or host.bus
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.state = SessionState.NEW
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._msg_id = 0

    def _emit(
        self,
        event_type: str,
        session_id: str,
        payload: Dict[str, Any],
        request_id: Optional[str] = None,
    ) -> None:
        self.bus.publish(
            SessionEvent(
                type=event_type, session_id=session_id, payload=payload, request_id=request_id
            )
        )

    def _set_state(self, state: SessionState) -> None:
        previous = self.state
        self.state = state
        logger.info("runtime state name=%s %s -> %s", self.name, previous.value, state.value)
        self.bus.publish(
            SessionEvent(
                type=STATE_CHANGED,
                session_id=self.name,
                payload={"from": previous.value, "to": state.value},
            )
        )

    async def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("runtime already started")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())
        reply = await self._request(
            wire.METHOD_INITIALIZE, {"protocol": "acp/1", "client": "fusion-harness"}
        )
        if not reply.get("ok"):
            await self.stop()
            raise RuntimeNotReady("adapter initialize rejected: %s" % reply)
        self._set_state(SessionState.READY)
        logger.info(
            "runtime started name=%s pid=%s adapter=%s",
            self.name,
            self._proc.pid,
            reply.get("adapter"),
        )

    async def stop(self) -> None:
        if self._proc is None:
            return
        self._set_state(SessionState.CLOSED)
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                logger.debug("reader loop ended name=%s", self.name)
            self._reader_task = None
        await self._proc.wait()
        self._proc = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("runtime stopped"))
        self._pending.clear()

    async def new_session(
        self, session_id: str, cwd: str = ".", env: Optional[Dict[str, str]] = None
    ) -> None:
        self._require_ready()
        self._set_state(SessionState.RUNNING)
        reply = await self._request(
            wire.METHOD_SESSION_NEW, {"session_id": session_id, "cwd": cwd, "env": env or {}}
        )
        if not reply.get("ok"):
            raise RuntimeError("session create failed: %s" % reply.get("error", "unknown"))
        self._emit(SESSION_NEW, session_id, {"cwd": cwd})

    async def prompt(self, session_id: str, text: str) -> Dict[str, Any]:
        self._require_ready()
        reply = await self._request(
            wire.METHOD_SESSION_PROMPT, {"session_id": session_id, "text": text}
        )
        if not reply.get("ok"):
            raise RuntimeError("prompt failed: %s" % reply.get("error", "unknown"))
        return reply

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                logger.warning("adapter stdout closed name=%s", self.name)
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                kind, msg = wire.decode_line(line)
            except wire.WireError as exc:
                logger.error("wire parse error name=%s code=%d", self.name, exc.code)
                continue
            if kind == "notification":
                self._route_notification(msg)
            elif kind == "request":
                asyncio.ensure_future(self._handle_driver_request(msg))
            elif kind == "response":
                future = self._pending.pop(int(msg["id"]), None)
                if future is not None and not future.done():
                    if "error" in msg:
                        future.set_result(
                            {"ok": False, "error": msg["error"].get("message", "error")}
                        )
                    else:
                        future.set_result({"ok": True, "result": msg.get("result")})
            else:
                logger.warning("unexpected wire message kind=%s name=%s", kind, self.name)

    def _route_notification(self, msg: Dict[str, Any]) -> None:
        params = msg.get("params") or {}
        method = msg.get("method")
        if method != wire.METHOD_SESSION_UPDATE:
            logger.debug("ignoring notification method=%s", method)
            return
        session_id = str(params.get("session_id") or "")
        update = params.get("update") or {}
        update_type = str(update.get("type") or "")
        event_type = _UPDATE_EVENT_MAP.get(update_type)
        if event_type is None:
            logger.warning("unknown update type=%s session=%s", update_type, session_id)
            return
        self._emit(event_type, session_id, dict(update))

    async def _handle_driver_request(self, msg: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        msg_id = msg["id"]
        method = str(msg.get("method"))
        params = dict(msg.get("params") or {})
        if method == wire.METHOD_PERMISSION_REQUEST:
            session_id = str(params.get("session_id") or "")
            decision = await self.host.request_permission(session_id, params)
            self._send_line(wire.encode_response(msg_id, decision))
        else:
            logger.warning("unsupported driver request method=%s — error reply", method)
            self._send_line(
                wire.encode_error(
                    msg_id, wire.METHOD_NOT_FOUND, "method not supported: %s" % method
                )
            )

    def _send_line(self, text: str) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            logger.error("cannot send: runtime stdin closed name=%s", self.name)
            return
        self._proc.stdin.write((text + "\n").encode("utf-8"))

    def _require_ready(self) -> None:
        if self.state not in (
            SessionState.READY,
            SessionState.RUNNING,
            SessionState.AWAITING_PERMISSION,
        ):
            raise RuntimeNotReady("runtime not ready state=%s" % self.state.value)

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None:
            raise RuntimeNotReady("runtime not started")
        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        self._send_line(wire.encode_request(msg_id, method, params))
        try:
            return await asyncio.wait_for(future, timeout=self.startup_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            logger.error("wire request timeout method=%s name=%s", method, self.name)
            raise
