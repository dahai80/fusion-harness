"""Team runtime: lifecycle state machine, task board, mailbox (issue #31, T1).

Deterministic mechanisms only — zero model calls. A message counts as
delivered only when its JSON file write succeeds (mailbox semantics);
inbox content is NEVER auto-injected into anyone's context — injection
timing is the consumer's call. Task claims are compare-and-set atomic.
Single member failure never cancels siblings (ParallelTeam precedent).
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fusion_harness.spawn_protocol import SpawnBrief, brief_from_event
from fusion_harness.subagent import SubagentResult, SubagentRunner, SubagentSpec
from fusion_harness.team import TeamDefinition, TeamMember

logger = logging.getLogger(__name__)

MSG_TYPES = ("text", "plan_approval", "shutdown_request", "shutdown_response")
_TASK_TRANSITIONS = {
    ("todo", "in_progress"),
    ("in_progress", "done"),
    ("todo", "blocked"),
    ("in_progress", "blocked"),
    ("blocked", "in_progress"),
    ("blocked", "done"),
}
_LIFECYCLE_EDGES = {
    ("DEFINED", "ACTIVE"),
    ("ACTIVE", "PAUSED"),
    ("PAUSED", "ACTIVE"),
    ("ACTIVE", "DISBANDED"),
    ("PAUSED", "DISBANDED"),
}


class TeamStateError(RuntimeError):
    """Illegal lifecycle or task-board transition."""


class TeamMessageError(ValueError):
    """Malformed mailbox message."""


@dataclass
class TeamTask:
    title: str
    detail: str = ""
    id: str = ""
    assignee: Optional[str] = None
    status: str = "todo"
    blocked_by: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    episodes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title or not isinstance(self.title, str):
            raise TeamStateError("task title required")
        if self.status not in ("todo", "in_progress", "blocked", "done"):
            raise TeamStateError("unknown task status: %s" % self.status)


@dataclass
class TeamMessage:
    type: str
    sender: str
    recipient: str
    body: str = ""

    def __post_init__(self) -> None:
        if self.type not in MSG_TYPES:
            raise TeamMessageError("unknown message type: %s" % self.type)
        if not self.sender or not self.recipient:
            raise TeamMessageError("sender and recipient required")


class TeamRuntime:
    _claim_lock = threading.Lock()

    def __init__(
        self,
        definition: TeamDefinition,
        runner: Optional[SubagentRunner] = None,
        bus=None,
        state_dir: str = "",
        clock: Callable[[], float] = None,
    ) -> None:
        self.definition = definition
        self.runner = runner or SubagentRunner(bus=bus)
        self.bus = bus
        self.state_dir = state_dir or os.path.join(".fusion-teams", definition.name)
        self._lifecycle = "DEFINED"
        self._tasks: Dict[str, TeamTask] = {}
        self._task_seq = 0
        self._inboxes: Dict[str, List[Dict[str, Any]]] = {m.name: [] for m in definition.members}
        import time as _time

        self._clock = clock or _time.time

    # ---------- lifecycle ----------

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    def _transition(self, target: str) -> None:
        if (self._lifecycle, target) not in _LIFECYCLE_EDGES:
            raise TeamStateError(
                "illegal lifecycle transition %s -> %s" % (self._lifecycle, target)
            )
        self._lifecycle = target
        self._emit("team.lifecycle", {"state": target})
        logger.info("team %s lifecycle -> %s", self.definition.name, target)

    def activate(self) -> None:
        self._transition("ACTIVE")

    def pause(self) -> None:
        self._transition("PAUSED")

    def resume(self) -> None:
        self._transition("ACTIVE")

    def disband(self, cleanup_inboxes: bool = True) -> None:
        self._transition("DISBANDED")
        if cleanup_inboxes:
            for name in list(self._inboxes):
                path = self._inbox_path(name)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError as exc:
                    logger.warning("inbox cleanup failed member=%s err=%s", name, exc)
        logger.info("team %s disbanded", self.definition.name)

    def _require_active(self) -> None:
        if self._lifecycle != "ACTIVE":
            raise TeamStateError("team not ACTIVE (state=%s)" % self._lifecycle)

    # ---------- task board ----------

    def add_task(self, task: TeamTask) -> str:
        self._require_active()
        self._task_seq += 1
        task.id = task.id or "%s-t%d" % (self.definition.name, self._task_seq)
        if task.id in self._tasks:
            raise TeamStateError("duplicate task id: %s" % task.id)
        self._tasks[task.id] = task
        self._emit("team.task.added", {"task_id": task.id, "title": task.title})
        return task.id

    def transition_task(self, task_id: str, target: str, blocked_by: List[str] = None) -> None:
        self._require_active()
        task = self._require_task(task_id)
        if (task.status, target) not in _TASK_TRANSITIONS:
            raise TeamStateError(
                "illegal task transition %s -> %s (task=%s)" % (task.status, target, task_id)
            )
        if target == "blocked" and not (blocked_by or task.blocked_by):
            raise TeamStateError("blocked requires non-empty blocked_by (task=%s)" % task_id)
        task.status = target
        if blocked_by is not None:
            task.blocked_by = list(blocked_by)
        self._emit("team.task.status", {"task_id": task_id, "status": target})

    def claim_task(self, task_id: str, member_name: str) -> bool:
        """Atomic compare-and-set claim: exactly one concurrent claimant wins."""
        self._require_active()
        task = self._require_task(task_id)
        self.definition.member(member_name)
        with self._claim_lock:
            if task.assignee is not None:
                logger.info("task claim lost task=%s claimant=%s", task_id, member_name)
                return False
            task.assignee = member_name
        self._emit("team.task.claimed", {"task_id": task_id, "member": member_name})
        return True

    def task(self, task_id: str) -> TeamTask:
        return self._require_task(task_id)

    def _require_task(self, task_id: str) -> TeamTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamStateError("unknown task: %s" % task_id)
        return task

    # ---------- member execution ----------

    async def run_member(
        self, member: TeamMember, task_id: str, brief: SpawnBrief, agent_fn
    ) -> SubagentResult:
        """Enforce the brief contract, stack member constraints (can only
        tighten), run through SubagentRunner; the result summary lands on
        the task board, never directly on the parent."""
        self._require_active()
        full_brief = brief if isinstance(brief, SpawnBrief) else brief_from_event(brief)
        budget = min(member.budget_tokens, full_brief.token_budget)
        spec = SubagentSpec(
            name=member.name,
            prompt=full_brief.objective,
            tool_allowlist=list(member.tool_allowlist),
            permission_mode=member.permission_mode,
            budget_tokens=budget,
            metadata={
                "task_id": task_id,
                "output_format": full_brief.output_format,
                "boundaries": full_brief.boundaries,
                "team": self.definition.name,
            },
        )
        self._emit("team.member.started", {"member": member.name, "task_id": task_id})
        result = await self.runner.run(spec, agent_fn)
        task = self._require_task(task_id)
        results = task.metadata.setdefault("results", [])
        results.append(
            {
                "member": member.name,
                "status": result.status,
                "summary": result.summary[:500],
                "tokens": result.tokens_spent,
            }
        )
        self._emit(
            "team.member.finished",
            {"member": member.name, "task_id": task_id, "status": result.status},
        )
        return result

    # ---------- mailbox ----------

    def _inbox_path(self, member_name: str) -> str:
        return os.path.join(self.state_dir, "inboxes", "%s.json" % member_name)

    def send(self, msg: TeamMessage) -> bool:
        """Delivered only when the inbox file write succeeds."""
        self.definition.member(msg.sender)
        self.definition.member(msg.recipient)
        record = asdict(msg)
        self._inboxes[msg.recipient].append(record)
        if not self._persist_inbox(msg.recipient):
            self._inboxes[msg.recipient].pop()
            logger.error("mailbox delivery failed recipient=%s", msg.recipient)
            return False
        self._emit("team.msg.sent", {"from": msg.sender, "to": msg.recipient, "type": msg.type})
        return True

    def broadcast(self, sender: str, body: str, type_: str = "text") -> int:
        """One delivery per member (sender excluded)."""
        self.definition.member(sender)
        delivered = 0
        for member in self.definition.members:
            if member.name == sender:
                continue
            if self.send(TeamMessage(type=type_, sender=sender, recipient=member.name, body=body)):
                delivered += 1
        return delivered

    def inbox(self, member_name: str) -> List[Dict[str, Any]]:
        self.definition.member(member_name)
        return list(self._inboxes[member_name])

    def _persist_inbox(self, member_name: str) -> bool:
        path = self._inbox_path(member_name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._inboxes[member_name], fh)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            logger.warning("inbox persist failed member=%s err=%s", member_name, exc)
            return False

    # ---------- persistence ----------

    def save_state(self, path: str) -> None:
        state = {
            "definition": {
                "name": self.definition.name,
                "namespace_key": self.definition.namespace_key,
                "lead_budget_tokens": self.definition.lead_budget_tokens,
            },
            "lifecycle": self._lifecycle,
            "task_seq": self._task_seq,
            "tasks": {tid: asdict(t) for tid, t in self._tasks.items()},
            "inboxes": self._inboxes,
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        logger.info("team state saved path=%s tasks=%d", path, len(self._tasks))

    def load_state(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if state["definition"]["name"] != self.definition.name:
            raise TeamStateError("state file belongs to team %s" % state["definition"]["name"])
        self._lifecycle = state["lifecycle"]
        self._task_seq = state["task_seq"]
        self._tasks = {tid: TeamTask(**data) for tid, data in state["tasks"].items()}
        self._inboxes = {m: list(v) for m, v in state["inboxes"].items()}
        logger.info("team state loaded path=%s tasks=%d", path, len(self._tasks))

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit("team", event, payload, session_id=self.definition.name)
        except Exception as exc:
            logger.warning("team lifecycle emit failed: %s", exc)


__all__ = [
    "TeamRuntime",
    "TeamTask",
    "TeamMessage",
    "TeamStateError",
    "TeamMessageError",
    "MSG_TYPES",
]
