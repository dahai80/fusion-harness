"""Sidecar protocol v1 (issue #110, G2).

Line-delimited JSON-RPC over stdio: consumers embed the kernel as a
subprocess instead of a library import. Fail-closed over the wire — an
internal exception becomes a typed error response, never "proceeding
unguarded" (the console handlers.go:335 class of bug, fixed at the
source). Approvals are kernel-owned: tool_admit returning
needs_approval=true issues an approval_id; resolve() re-evaluates in
the kernel with can_confirm=True so permission/budget/hook stages
still apply on the confirm path.

Wire format: one JSON object per line on stdin, one response line per
request on stdout, {"method":"event",...} notification lines
interleaved, diagnostics on stderr. hello mismatch = protocol error
and the recommended client behavior is exit 2.
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

from .compactor import Compactor
from .funnels import LifecycleBus, LLMFunnel, ToolFunnel
from .observability import ObservabilityLoop
from .permissions import ConfirmationPolicy, PermissionEngine

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
KERNEL_METHODS = [
    "ping", "hello", "pre_llm", "post_llm", "tool_admit", "resolve", "meter", "reset", "report",
]
FEATURES = ["approvals", "meter", "compaction", "events", "observability"]
ERROR_CODES = ["protocol", "state", "budget", "permission", "internal"]


def _resolve_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=__file__.rsplit("/", 2)[0],
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("commit resolve failed: %s", e)
    return "unknown"


class BudgetStub:
    """Minimal budget_ref honoring the kernel contract: is_exceeded /
    record_usage / spent_tokens. Replace via reset(budget=...) for
    consumers with real accounting."""

    def __init__(self, limit_tokens: int = 0) -> None:
        self.limit_tokens = int(limit_tokens)
        self.spent_tokens = 0

    def is_exceeded(self, graph_id: str = "") -> bool:
        return self.limit_tokens > 0 and self.spent_tokens >= self.limit_tokens

    def record_usage(self, prompt_tokens: int, completion_tokens: int, graph_id: str = "") -> None:
        self.spent_tokens += int(prompt_tokens) + int(completion_tokens)

    def add(self, tokens: int) -> None:
        self.spent_tokens += int(tokens)


@dataclass
class SidecarState:
    bus: LifecycleBus = field(default_factory=LifecycleBus)
    compactor: Compactor = field(default_factory=Compactor)
    budget: BudgetStub = field(default_factory=BudgetStub)
    # Fail-closed sidecar default: DEFAULT mode + tier=risk confirmation
    # policy (threshold 0.5 so the network band asks). Risky calls come
    # back needs_approval with an approval_id (never raw-allowed);
    # low-risk calls auto-allow so reads stay quiet; dangerous patterns
    # are content-scan DENIED with no approval path. Hosts relax via
    # reset(allow_tools=[...]) (tier=never overrides).
    permission_mode: str = "default"
    allow_tools: list = field(default_factory=list)
    permissions: PermissionEngine = field(default=None)
    tool_funnel: ToolFunnel = field(default=None)
    llm_funnel: LLMFunnel = field(default=None)
    pending_approvals: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.permissions is None:
            policy = ConfirmationPolicy(tier="risk", risk_threshold=0.5)
            for tool in self.allow_tools:
                policy.tool_overrides[tool] = "never"
            self.permissions = PermissionEngine(
                mode=self.permission_mode, confirmation_policy=policy
            )
        if self.tool_funnel is None:
            self.tool_funnel = ToolFunnel(
                permissions=self.permissions,
                lifecycle_bus=self.bus,
                budget_ref=self.budget,
            )
        if self.llm_funnel is None:
            self.llm_funnel = LLMFunnel(
                lifecycle_bus=self.bus,
                compactor=self.compactor,
                budget_ref=self.budget,
            )
        # G4 observability loop: bus events land as normalized records;
        # report() = meter snapshot + (agent, step, mode) attribution.
        self.observer = ObservabilityLoop(
            run_id="sidecar", budget_ref=self.budget, llm_funnel=self.llm_funnel
        )
        self.observer.observe_bus(self.bus)


def error_resp(code: str, message: str, req_id=None) -> dict:
    return {"id": req_id, "error": {"code": code, "message": message}}


class Harnessd:
    """Protocol v1 dispatch table. handle() never raises: any internal
    exception is returned as a typed internal error (fail-closed)."""

    def __init__(self, state: SidecarState | None = None, kernel_version: str = "") -> None:
        self._state = state or SidecarState()
        self.kernel_version = kernel_version
        self._hello_ok = False
        self._loop = asyncio.new_event_loop()
        self.on_event = None  # optional callable(ev_dict) for the event channel

    @property
    def state(self) -> SidecarState:
        return self._state

    def handle_sync(self, req: dict) -> dict:
        return self._loop.run_until_complete(self.handle(req))

    async def handle(self, req: dict) -> dict:
        req_id = req.get("id")
        method = req.get("method", "")
        try:
            if method == "hello":
                return self._hello(req)
            if not self._hello_ok:
                return error_resp("protocol", "hello required before %s" % method, req_id)
            handler = {
                "ping": self._ping,
                "pre_llm": self._pre_llm,
                "post_llm": self._post_llm,
                "tool_admit": self._tool_admit,
                "resolve": self._resolve,
                "meter": self._meter,
                "reset": self._reset,
                "report": self._report,
            }.get(method)
            if handler is None:
                return error_resp("protocol", "unknown method: %s" % method, req_id)
            return await handler(req)
        except Exception as e:  # fail-closed: never crash the wire
            logger.exception("sidecar internal error method=%s", method)
            return error_resp("internal", "internal error in %s: %s" % (method, e), req_id)

    def _hello(self, req: dict) -> dict:
        req_id = req.get("id")
        params = req.get("params") or {}
        client_v = params.get("protocol_version")
        if client_v != PROTOCOL_VERSION:
            logger.warning("hello version mismatch client=%s kernel=%s", client_v, PROTOCOL_VERSION)
            return error_resp(
                "protocol",
                "protocol version mismatch: client=%s kernel=%s" % (client_v, PROTOCOL_VERSION),
                req_id,
            )
        self._hello_ok = True
        return {
            "id": req_id,
            "result": {
                "protocol_version": PROTOCOL_VERSION,
                "kernel_version": self.kernel_version,
                "commit": _resolve_commit(),
                "methods": KERNEL_METHODS,
                "features": FEATURES,
            },
        }

    async def _ping(self, req: dict) -> dict:
        return {"id": req.get("id"), "result": {"pong": True}}

    async def _pre_llm(self, req: dict) -> dict:
        p = req.get("params") or {}
        self._state.observer.record(
            "llm_before", str(p.get("node_label", "")),
            {"model": p.get("model", ""), "messages": len(p.get("messages") or [])},
            run_id=str(p.get("graph_id", "")) or None,
        )
        ctx = _Ctx(p)
        proceed, reason, msgs = await self._state.llm_funnel.pre_llm(
            ctx,
            node_label=str(p.get("node_label", "")),
            model=str(p.get("model", "")),
            messages=list(p.get("messages") or []),
        )
        budget = self._state.budget
        return {
            "id": req.get("id"),
            "result": {
                "proceed": bool(proceed),
                "reason": reason,
                "messages": msgs,
                "spent_tokens": getattr(budget, "spent_tokens", None),
                "limit_tokens": getattr(budget, "limit_tokens", None),
            },
        }

    async def _post_llm(self, req: dict) -> dict:
        p = req.get("params") or {}
        parse_error = bool(p.get("parse_error", False))
        finish_reason = str(p.get("finish_reason", ""))
        if parse_error or finish_reason in ("length", "max_output_tokens", "truncated"):
            self._state.observer.record(
                "error", str(p.get("node_label", "")),
                {"kind": "parse_error" if parse_error else "truncation", "finish_reason": finish_reason},
                run_id=str(p.get("graph_id", "")) or None,
            )
        self._state.llm_funnel.post_llm(
            _Ctx(p),
            node_label=str(p.get("node_label", "")),
            model=str(p.get("model", "")),
            usage=p.get("usage") or {},
            content=str(p.get("content", "")),
            finish_reason=str(p.get("finish_reason", "")),
            parse_error=bool(p.get("parse_error", False)),
        )
        return {"id": req.get("id"), "result": {"ok": True}}

    async def _tool_admit(self, req: dict) -> dict:
        p = req.get("params") or {}
        # can_confirm=True: the sidecar CAN pause — the approval round-trip
        # (needs_approval -> host -> resolve) IS the pause. Risky calls ask,
        # low-risk calls auto-allow, dangerous patterns deny outright.
        outcome = await self._state.tool_funnel.admit(
            _Ctx(p),
            tool_name=str(p.get("tool_name", "")),
            args=dict(p.get("args") or {}),
            category=str(p.get("category", "tool_call")),
            node_label=str(p.get("node_label", "")),
            content_repr=str(p.get("content_repr", "")),
            can_confirm=True,
        )
        result = outcome.to_dict()
        tool_name = str(p.get("tool_name", ""))
        self._state.observer.record(
            "tool_call", str(p.get("node_label", "")),
            {"tool": tool_name, "allowed": outcome.allowed,
             "needs_approval": outcome.needs_approval},
            args=p.get("args") or {},
            run_id=str(p.get("graph_id", "")) or None,
        )
        if not outcome.allowed and not outcome.needs_approval:
            self._state.observer.record(
                "error", str(p.get("node_label", "")),
                {"kind": "tool_denied", "tool": tool_name, "reason": outcome.reason},
                run_id=str(p.get("graph_id", "")) or None,
            )
        if outcome.needs_approval:
            approval_id = uuid.uuid4().hex
            self._state.pending_approvals[approval_id] = {"params": p}
            result["approval_id"] = approval_id
        return {"id": req.get("id"), "result": result}

    async def _resolve(self, req: dict) -> dict:
        p = req.get("params") or {}
        approval_id = str(p.get("approval_id", ""))
        entry = self._state.pending_approvals.pop(approval_id, None)
        if entry is None:
            return error_resp("state", "unknown approval_id: %s" % approval_id, req.get("id"))
        approve = bool(p.get("approve", False))
        params = entry["params"]
        tool_name = str(params.get("tool_name", ""))
        if not approve:
            logger.info("approval denied id=%s tool=%s", approval_id, tool_name)
            return {
                "id": req.get("id"),
                "result": {
                    "allowed": False,
                    "blocked_by": "human_gate",
                    "reason": "approval denied",
                },
            }
        # Kernel-owned approval: grant tier=never for this tool (session
        # scope), then re-run the FULL admission chain — budget, hooks and
        # the plan gate still apply on the confirm path.
        self._state.permissions.confirmation_policy.tool_overrides[tool_name] = "never"
        outcome = await self._state.tool_funnel.admit(
            _Ctx(params),
            tool_name=tool_name,
            args=dict(params.get("args") or {}),
            category=str(params.get("category", "tool_call")),
            node_label=str(params.get("node_label", "")),
            content_repr=str(params.get("content_repr", "")),
            can_confirm=True,
        )
        result = outcome.to_dict()
        if outcome.allowed:
            result["granted_tool"] = tool_name
        else:
            # Still denied (content scan / plan gate / budget): revoke the
            # grant so an approved-but-blocked tool never stays bypassed.
            self._state.permissions.confirmation_policy.tool_overrides.pop(tool_name, None)
            result["granted_tool"] = ""
        return {"id": req.get("id"), "result": result}

    async def _meter(self, req: dict) -> dict:
        meter = self._state.llm_funnel.meter()
        budget = self._state.budget
        meter["budget_spent_tokens"] = getattr(budget, "spent_tokens", None)
        meter["budget_exceeded"] = bool(budget.is_exceeded())
        return {"id": req.get("id"), "result": meter}

    async def _report(self, req: dict) -> dict:
        # G4: one report — meter snapshot + failure attribution over the
        # recorded run. Attribution errors degrade to an "error" key, never
        # break the wire.
        return {"id": req.get("id"), "result": self._state.observer.report()}

    async def _reset(self, req: dict) -> dict:
        p = req.get("params") or {}
        budget = BudgetStub(limit_tokens=int(p.get("limit_tokens", 0)))
        self._state = SidecarState(
            budget=budget,
            permission_mode=str(p.get("mode", "default")),
            allow_tools=list(p.get("allow_tools") or []),
        )
        logger.info(
            "sidecar state reset limit=%s mode=%s allow=%s",
            budget.limit_tokens,
            self._state.permission_mode,
            self._state.allow_tools,
        )
        return {"id": req.get("id"), "result": {"ok": True}}


class _Ctx:
    """Minimal ctx satisfying funnel getattr probing (graph_id, role, plan_mode)."""

    def __init__(self, params: dict) -> None:
        self.graph_id = str(params.get("graph_id", ""))
        self.node_label = str(params.get("node_label", ""))
        self.role = str(params.get("role", ""))
        self.plan_mode = bool(params.get("plan_mode", False))
        self.session_id = str(params.get("session_id", ""))


def serve(harness: Harnessd, stdin=None, stdout=None) -> int:
    """Line loop. Malformed JSON goes to stderr, never crashes; EOF ends."""
    fin = stdin if stdin is not None else sys.stdin
    fout = stdout if stdout is not None else sys.stdout
    if harness.on_event is not None:
        harness.state.bus.subscribe(lambda ev: _emit_event(fout, harness.on_event, ev))
    count = 0
    for line in fin:
        line = line.strip()
        if not line:
            continue
        count += 1
        try:
            req = json.loads(line)
        except ValueError as e:
            logger.warning("malformed request line %s: %s", count, e)
            print(
                json.dumps(error_resp("protocol", "malformed JSON: %s" % e, None)),
                file=fout,
                flush=True,
            )
            continue
        resp = harness.handle_sync(req)
        print(json.dumps(resp, ensure_ascii=False), file=fout, flush=True)
    logger.info("sidecar EOF after %s requests", count)
    return 0


def _emit_event(fout, on_event, ev) -> None:
    try:
        payload = on_event(ev)
        if payload is not None:
            print(
                json.dumps({"method": "event", "params": payload}, ensure_ascii=False),
                file=fout,
                flush=True,
            )
    except Exception as e:  # notify path: never break the request loop
        logger.warning("event emit failed: %s", e)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnessd")
    parser.add_argument("--version", action="store_true", help="print protocol version and exit")
    parser.add_argument("--conformance", action="store_true", help="run self-check vectors and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s"
    )
    if args.version:
        print(json.dumps({"protocol_version": PROTOCOL_VERSION}))
        return 0
    if args.conformance:
        from .sidecar_conformance import run_self_check

        return run_self_check()
    from . import KERNEL_VERSION

    harness = Harnessd(kernel_version=KERNEL_VERSION)
    return serve(harness)


if __name__ == "__main__":
    sys.exit(main())
