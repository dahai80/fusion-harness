"""Issue #150: optional fusion-executor backend for SandboxPolicy."""

import sys

import pytest

from fusion_harness import SandboxPolicy, ExecutionTier
from fusion_harness.sandbox import SandboxRunnerUnavailable
from fusion_harness.executor_backend import (
    FusionExecutorBackend,
    select_sandbox_runner,
)
from fusion_harness.sandbox import check_runner_conformance


class _FakeNativeExecutor:
    def __init__(self, exit_code=0, rollback_ok=True):
        self.exit_code = exit_code
        self.rollback_ok = rollback_ok
        self.runs = []
        self.snapshots = []
        self.rollbacks = []
        self._seq = 0

    def snapshot_create(self, cwd):
        self._seq += 1
        sid = "snap-%d" % self._seq
        self.snapshots.append((sid, cwd))
        return sid

    def rollback(self, snapshot_id, cwd):
        self.rollbacks.append((snapshot_id, cwd))
        return self.rollback_ok

    def run(self, command, cwd=None, timeout_sec=30.0, **kw):
        self.runs.append({"command": command, "cwd": cwd, "timeout_sec": timeout_sec})
        return type(
            "R", (), {"exit_code": self.exit_code, "stdout": "out-ok", "stderr": "err-txt"}
        )()


def _backend(tmp_path, **kw):
    fake = _FakeNativeExecutor(**kw)
    return FusionExecutorBackend(cwd=str(tmp_path), executor=fake), fake


class TestAdapterContract:
    def test_satisfies_runner_protocol(self, tmp_path):
        backend, _ = _backend(tmp_path)
        assert check_runner_conformance(backend) == []

    async def test_run_sandboxed_returns_output(self, tmp_path):
        backend, fake = _backend(tmp_path)
        out = await backend.run_sandboxed("shell.run", {"command": "pytest -q"})
        assert out == "out-ok"
        assert fake.runs[0]["command"] == "pytest -q"
        assert fake.runs[0]["cwd"] == str(tmp_path)

    async def test_snapshot_created_before_run(self, tmp_path):
        backend, fake = _backend(tmp_path)
        await backend.run_sandboxed("shell.run", {"command": "make"})
        assert len(fake.snapshots) == 1
        assert backend.last_snapshot_id == fake.snapshots[0][0]

    async def test_no_command_fails_closed(self, tmp_path):
        backend, _ = _backend(tmp_path)
        with pytest.raises(SandboxRunnerUnavailable):
            await backend.run_sandboxed("shell.run", {})

    async def test_nonzero_exit_raises_unavailable(self, tmp_path):
        backend, _ = _backend(tmp_path, exit_code=2)
        with pytest.raises(SandboxRunnerUnavailable, match="rc=2"):
            await backend.run_sandboxed("shell.run", {"command": "boom"})


class TestRollback:
    async def test_rollback_restores_last_snapshot(self, tmp_path):
        backend, fake = _backend(tmp_path)
        await backend.run_sandboxed("shell.run", {"command": "mutate"})
        assert backend.rollback() is True
        assert fake.rollbacks == [(backend.last_snapshot_id, str(tmp_path))]

    def test_rollback_explicit_snapshot_id(self, tmp_path):
        backend, fake = _backend(tmp_path)
        sid = backend.snapshot()
        assert backend.rollback(sid) is True
        assert fake.rollbacks == [(sid, str(tmp_path))]

    def test_rollback_without_snapshot_returns_false(self, tmp_path):
        backend, _ = _backend(tmp_path)
        assert backend.rollback() is False

    def test_rollback_failure_propagates_false(self, tmp_path):
        backend, _ = _backend(tmp_path, rollback_ok=False)
        backend.snapshot()
        assert backend.rollback() is False


class TestSelection:
    def test_missing_package_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "fusion_executor", None)
        default = object()
        runner, name = select_sandbox_runner(preferred="fusion_executor", default_runner=default)
        assert runner is default
        assert name == "default"

    def test_default_request_skips_adapter(self):
        default = object()
        runner, name = select_sandbox_runner(preferred="default", default_runner=default)
        assert runner is default
        assert name == "default"

    def test_policy_with_fallback_runner_denies_without_runner(self):
        policy = SandboxPolicy(runner=None)
        decision = policy.decide("shell.run", {"command": "x"}, (0.95, "destructive"))
        assert decision.tier is ExecutionTier.BLOCKED
