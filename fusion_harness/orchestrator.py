"""Thin orchestration primitives over SubagentRunner (plan follow-up: the
orchestration ladder, kernel-owned determinstic half).

Scope boundary (contract): the kernel owns two deterministic primitives —
- BackgroundPool: fire-and-collect; each spawn is one SubagentRunner.run
  scheduled as a task; summary-only return is inherited, not re-implemented.
- ParallelTeam: fan-out a fixed spec set concurrently; per-subagent budgets
  stay isolated (runner's ledger_factory); one child failing never cancels
  siblings — failures come back as status="failed" results.

Workflow/DAG orchestration stays consumer-side (fusion-cowork owns the DAG
engine); anything needing model judgment stays consumer-side too. This
module adds zero policy — it only shapes concurrency.
"""

import asyncio
import logging
from typing import List, Optional

from fusion_harness.subagent import AgentFn, SubagentResult, SubagentRunner, SubagentSpec

logger = logging.getLogger(__name__)


class BackgroundPool:
    def __init__(self, runner: SubagentRunner) -> None:
        self.runner = runner
        self._tasks: List[asyncio.Task] = []

    def spawn(
        self,
        spec: SubagentSpec,
        agent_fn: AgentFn,
        parent_tools: Optional[List[str]] = None,
        parent_mode: str = "default",
    ) -> asyncio.Task:
        task = asyncio.ensure_future(
            self.runner.run(spec, agent_fn, parent_tools=parent_tools, parent_mode=parent_mode)
        )
        self._tasks.append(task)
        logger.info("background spawn name=%s pool_size=%d", spec.name, len(self._tasks))
        return task

    async def collect(self) -> List[SubagentResult]:
        """Await every spawned task; a crashed agent_fn becomes a failed
        result (never raises out of collect — SubagentRunner.run already
        converts exceptions, this is the belt for cancelled/hard failures)."""
        results = []
        for task in self._tasks:
            try:
                results.append(await task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("background task escaped runner: %s", exc)
                results.append(
                    SubagentResult(
                        subagent_id="unknown", name="unknown", status="failed", error=str(exc)
                    )
                )
        self._tasks.clear()
        logger.info("background collect results=%d", len(results))
        return results


class ParallelTeam:
    def __init__(self, runner: SubagentRunner) -> None:
        self.runner = runner

    async def run(
        self,
        specs: List[SubagentSpec],
        agent_fn: AgentFn,
        parent_tools: Optional[List[str]] = None,
        parent_mode: str = "default",
    ) -> List[SubagentResult]:
        if not specs:
            return []
        tasks = [
            asyncio.ensure_future(
                self.runner.run(spec, agent_fn, parent_tools=parent_tools, parent_mode=parent_mode)
            )
            for spec in specs
        ]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        results: List[SubagentResult] = []
        for spec, item in zip(specs, raw):
            if isinstance(item, Exception):
                logger.error("team member failed name=%s err=%s", spec.name, item)
                results.append(
                    SubagentResult(
                        subagent_id="unknown", name=spec.name, status="failed", error=str(item)
                    )
                )
            else:
                results.append(item)
        failed = sum(1 for r in results if r.status == "failed")
        logger.info("team done members=%d failed=%d", len(results), failed)
        return results


__all__ = ["BackgroundPool", "ParallelTeam"]
