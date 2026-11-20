"""Optional fusion-executor backend for SandboxPolicy (issue #150).

User ruling 2026-11-20: fusion-executor (native macOS sandbox + PTY +
Git-snapshot rollback) becomes an optional execution backend. The
adapter is an optional extra: the `fusion_executor` import is lazy and
lives only inside construction — absent package means a typed refusal
and selection falls back to the default runner. Core paths gain zero
dependencies; the 0-runtime-dep invariant holds. Factory impl nodes
select the executor backend when available; other consumers are
unaffected.

Rollback policy: the adapter snapshots (Git snapshot via the executor)
before each sandboxed run and keeps the last snapshot id; a failed impl
node restores pre-node state via `rollback(snapshot_id)`.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

from fusion_harness.sandbox import SandboxRunnerUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutorBackendUnavailable",
    "FusionExecutorBackend",
    "select_sandbox_runner",
]


class ExecutorBackendUnavailable(RuntimeError):
    """fusion-executor backend could not be constructed (package absent or
    native extension not built). Callers must fall back to the default
    runner or deny — never run raw."""


class FusionExecutorBackend:
    """SandboxRunner adapter over `fusion_executor.FusionSandboxExecutor`.
    Satisfies the SandboxRunner protocol (async run_sandboxed) and adds
    Git-snapshot rollback: `snapshot()` before a run, `rollback(id)` to
    restore pre-node state. An injected executor (test doubles, pre-built
    instances) bypasses the lazy import."""

    def __init__(
        self,
        cwd: str = "",
        timeout_s: float = 30.0,
        snapshot_before: bool = True,
        executor: Any = None,
    ) -> None:
        if executor is None:
            try:
                from fusion_executor import FusionSandboxExecutor
            except Exception as exc:
                raise ExecutorBackendUnavailable(
                    "fusion-executor backend unavailable: %s" % exc
                ) from exc
            executor = FusionSandboxExecutor()
        self._executor = executor
        self.cwd = cwd or os.getcwd()
        self.timeout_s = timeout_s
        self.snapshot_before = snapshot_before
        self.last_snapshot_id = ""

    async def run_sandboxed(self, tool_name: str, args: Dict[str, Any]) -> Any:
        command = str(args.get("command") or "")
        if not command:
            raise SandboxRunnerUnavailable("no command in args for %s" % tool_name)
        if self.snapshot_before:
            self.last_snapshot_id = await asyncio.to_thread(
                self._executor.snapshot_create, self.cwd
            )
            logger.info(
                "executor backend snapshot created tool=%s snapshot=%s",
                tool_name,
                self.last_snapshot_id,
            )
        result = await asyncio.to_thread(
            self._executor.run, command, cwd=self.cwd, timeout_sec=self.timeout_s
        )
        exit_code = getattr(result, "exit_code", 0)
        output = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        if exit_code:
            raise SandboxRunnerUnavailable(
                "executor backend call failed rc=%s err=%s" % (exit_code, stderr[:200])
            )
        return output

    def snapshot(self) -> str:
        """Explicit pre-node snapshot (factory impl node entry)."""
        self.last_snapshot_id = self._executor.snapshot_create(self.cwd)
        return self.last_snapshot_id

    def rollback(self, snapshot_id: str = "") -> bool:
        """Restore pre-node state from a Git snapshot; empty = last snapshot.
        Returns False when the executor reports failure — callers decide
        whether to fail the node."""
        sid = snapshot_id or self.last_snapshot_id
        if not sid:
            logger.warning("rollback requested with no snapshot id")
            return False
        ok = bool(self._executor.rollback(sid, self.cwd))
        logger.info("executor backend rollback snapshot=%s ok=%s", sid, ok)
        return ok


def select_sandbox_runner(
    preferred: str = "default",
    default_runner: Any = None,
    backend_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, str]:
    """Backend selection with deterministic fallback (issue #150). Returns
    (runner, backend_name). 'fusion_executor' tries the adapter; a typed
    construction failure logs a warning and falls back to default_runner
    (None = no runner wired — SandboxPolicy denies sandbox-tier calls,
    same as today)."""
    if preferred == "fusion_executor":
        try:
            return FusionExecutorBackend(**(backend_kwargs or {})), "fusion_executor"
        except ExecutorBackendUnavailable as exc:
            logger.warning("fusion-executor backend unavailable, falling back: %s", exc)
    return default_runner, "default"
