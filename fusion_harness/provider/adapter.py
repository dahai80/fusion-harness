"""ProviderAdapter contract — one adapter per agent runtime, host is runtime-agnostic (#15).

Pattern adopted from t3code provider-core server seam. An adapter owns one
agent runtime (subprocess CLI, local daemon, ...) and exposes session
lifecycle + prompt + permission callbacks. The host never knows runtimes
beyond this contract. Transport-agnostic: no wire details here.
"""

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

from fusion_harness.provider.events import EventBus, SessionEvent

logger = logging.getLogger(__name__)

PermissionHandler = Callable[[Dict[str, Any]], Any]
# PermissionHandler receives the request payload (tool, arguments, session_id)
# and returns a truthy decision dict, e.g. {"allow": True, "mode": "default"}.
# Async handlers are supported via AdapterHost._maybe_await.


class ProviderAdapter(ABC):
    """Contract every agent-runtime adapter implements. Zero harness changes
    expected when a new runtime arrives — implement this, register with host."""

    name: str = "abstract"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def new_session(self, session_id: str, cwd: str, env: Dict[str, str]) -> None: ...

    @abstractmethod
    async def prompt(self, session_id: str, text: str) -> None: ...

    @abstractmethod
    async def respond_permission(self, session_id: str, request_id: str, allow: bool) -> None: ...

    @abstractmethod
    async def close_session(self, session_id: str) -> None: ...


class AdapterHost:
    """Runtime-agnostic registry + event routing. Single owner of ids and the
    permission callback seam (HiTL round-trips land here, funnels untouched)."""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        permission_handler: Optional[PermissionHandler] = None,
    ) -> None:
        self._adapters: Dict[str, ProviderAdapter] = {}
        self._lock = threading.Lock()
        self.bus = bus or EventBus()
        self.permission_handler = permission_handler
        self._counter = 0

    def register(self, adapter: ProviderAdapter) -> None:
        with self._lock:
            if adapter.name in self._adapters:
                raise ValueError("adapter already registered: %s" % adapter.name)
            self._adapters[adapter.name] = adapter
        logger.info("adapter registered name=%s", adapter.name)

    def adapter(self, name: str) -> ProviderAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError("no adapter registered: %s" % name) from None

    def adapters(self) -> Dict[str, ProviderAdapter]:
        return dict(self._adapters)

    def allocate_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            value = self._counter
        return "%s-%d" % (prefix, value)

    def emit(self, event: SessionEvent) -> None:
        self.bus.publish(event)

    async def request_permission(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route a permission request through the injected handler (HiTL).
        Fail-closed: no handler or handler error = deny."""
        request_id = str(payload.get("request_id") or self.allocate_id("perm"))
        payload = {**payload, "session_id": session_id, "request_id": request_id}
        self.emit(
            SessionEvent(
                type="session/permission_request",
                session_id=session_id,
                payload=payload,
                request_id=request_id,
            )
        )
        if self.permission_handler is None:
            logger.warning("permission denied: no handler wired session=%s", session_id)
            return {"allow": False, "reason": "no permission handler"}
        try:
            decision = self.permission_handler(payload)
            if asyncio.iscoroutine(decision):
                decision = await decision
            decision = dict(decision or {})
        except Exception:
            logger.exception("permission handler failed session=%s — deny", session_id)
            return {"allow": False, "reason": "permission handler error"}
        decision.setdefault("allow", False)
        self.emit(
            SessionEvent(
                type="session/permission_response",
                session_id=session_id,
                payload=decision,
                request_id=request_id,
            )
        )
        logger.info(
            "permission decided session=%s request=%s allow=%s",
            session_id,
            request_id,
            decision.get("allow"),
        )
        return decision
