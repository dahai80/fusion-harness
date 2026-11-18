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
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fusion_harness.session_log import SessionEventLog
from fusion_harness.spawn_protocol import SpawnBrief, brief_from_event
from fusion_harness.subagent import SubagentResult, SubagentRunner, SubagentSpec
from fusion_harness.team import TeamDefError, TeamDefinition, TeamMember

logger = logging.getLogger(__name__)

TEAM_SPEC_CAP_BYTES = 4096
MEMBER_CONTEXT_CAP_BYTES = 2048

# Issue #63: per-run event stream event types (tool_* come from session_log).
RUN_STREAM_DIR = "runs"

_REDACT_KEYS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)
_REDACT_VALUE_RE = re.compile(
    r"(?i)\b([\w.-]*(?:secret|token|password|passwd|api[_-]?key|credential)[\w.-]*\s*[=:]\s*)(\S+)"
)

MSG_TYPES = ("text", "plan_approval", "shutdown_request", "shutdown_response")
_TASK_STATUSES = ("todo", "queued", "in_progress", "blocked", "done", "cancelled")
_TASK_TRANSITIONS = {
    ("todo", "in_progress"),
    ("in_progress", "done"),
    ("todo", "blocked"),
    ("in_progress", "blocked"),
    ("blocked", "in_progress"),
    ("blocked", "done"),
    # Wakeup rules (issue #65): a fired wakeup returns a blocked task to
    # the lead's pick pool; an `end` timeout cancels it.
    ("blocked", "queued"),
    ("queued", "in_progress"),
    ("blocked", "cancelled"),
}
WAKEUP_CAP_PER_TASK = 8
_WAKEUP_KINDS = ("at", "every")
_WAKEUP_TIMEOUT_ACTIONS = ("wake", "end")
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


def _redact(obj: Any) -> Any:
    """Issue #63 redaction: secret-looking values inside commands and tool
    args become `***` before anything is written to the run stream. Two
    passes: sensitive key names redact the whole value; embedded
    `KEY=value` / `KEY: value` patterns inside string values redact the
    value part only."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and any(marker in key.lower() for marker in _REDACT_KEYS):
                out[key] = "***"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    if isinstance(obj, str):
        return _REDACT_VALUE_RE.sub(lambda m: "%s***" % m.group(1), obj)
    return obj


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
        if self.status not in _TASK_STATUSES:
            raise TeamStateError("unknown task status: %s" % self.status)


@dataclass
class TeamMessage:
    type: str
    sender: str
    recipient: str
    body: str = ""
    msg_id: str = ""
    reply_to: str = ""
    mentions: tuple = ()
    attachments: tuple = ()
    audio_ref: str = ""
    transcript: str = ""

    def __post_init__(self) -> None:
        if self.type not in MSG_TYPES:
            raise TeamMessageError("unknown message type: %s" % self.type)
        if not self.sender or not self.recipient:
            raise TeamMessageError("sender and recipient required")
        if self.reply_to and not self.reply_to.startswith("msg_"):
            raise TeamMessageError(
                "reply_to must be empty or a msg_id (msg_...): %s" % self.reply_to
            )
        if any(not isinstance(m, str) or not m for m in self.mentions):
            raise TeamMessageError("mentions must be non-empty member-name strings")
        if any(not isinstance(a, str) or not a for a in self.attachments):
            raise TeamMessageError("attachments must be non-empty artifact-ref strings")


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
        self._supplement_queues: Dict[str, Any] = {}
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

    def transition_task(
        self, task_id: str, target: str, blocked_by: List[str] = None, reason: str = ""
    ) -> None:
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
        if reason:
            task.metadata["cancel_reason"] = reason
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
        self._prune_expired_wakeups(task)
        return task

    def _prune_expired_wakeups(self, task: TeamTask) -> None:
        """Lazy prune (issue #65): expired rules drop on task read — no
        separate sweeper, no new retention policy."""
        rules = task.metadata.get("wakeups")
        if not rules:
            return
        now = self._clock()
        kept = [r for r in rules if r.get("expires_at") is None or r["expires_at"] > now]
        if len(kept) != len(rules):
            logger.info("wakeups pruned task=%s removed=%d", task.id, len(rules) - len(kept))
            task.metadata["wakeups"][:] = kept

    # ---------- member execution ----------

    def _runs_dir(self, task_id: str) -> str:
        return os.path.join(self.state_dir, RUN_STREAM_DIR, task_id)

    def _next_attempt(self, task_id: str) -> int:
        try:
            names = os.listdir(self._runs_dir(task_id))
        except OSError:
            return 1
        attempts = []
        for name in names:
            if name.endswith(".jsonl") and name[:-6].isdigit():
                attempts.append(int(name[:-6]))
        return (max(attempts) + 1) if attempts else 1

    def _open_run_stream(self, task_id: str, attempt: int) -> Optional[SessionEventLog]:
        """Issue #63: one JSONL per run at <state_dir>/runs/<task_id>/<attempt>.jsonl.
        The stream's session_id is the attempt number (the JSONL filename
        convention of SessionEventLog); task_id/member/team ride every
        event payload. A stream that cannot be opened never fails the run."""
        try:
            os.makedirs(self._runs_dir(task_id), exist_ok=True)
            return SessionEventLog(session_id=str(attempt), log_dir=self._runs_dir(task_id))
        except OSError as exc:
            logger.warning("run stream open failed task=%s err=%s", task_id, exc)
            return None

    def _usage_snapshot(self, session: "Any") -> Dict[str, int]:
        """run_finished usage straight from the funnel ledger (tolerance 0
        against funnels totals — no re-aggregation). Falls back to the
        session counter when no ledger is wired."""
        ledger = getattr(session, "ledger", None)
        if ledger is not None:
            bal = ledger.balance(scope=getattr(session, "subagent_id", None))
            return {
                "prompt_tokens": int(bal.get("prompt_tokens", 0)),
                "completion_tokens": int(bal.get("completion_tokens", 0)),
                "reasoning_tokens": int(bal.get("reasoning_tokens", 0)),
                "total_tokens": int(bal.get("total_tokens", 0)),
            }
        return {"total_tokens": int(getattr(session, "tokens_spent", 0))}

    @staticmethod
    def _truncate_utf8(text: str, cap: int) -> str:
        data = text.encode("utf-8")[:cap]
        return data.decode("utf-8", errors="ignore")

    def _assemble_member_prompt(self, member: TeamMember, objective: str) -> str:
        """Issue #61: resident per-member context assembly. Members are
        in-process runs with no cwd and no AGENTS.md load, so the lead's
        brief objective is their entire per-round context. Prepend the
        team spec (SPEC.md, re-read every dispatch so edits apply without
        restart) and the member's resident context field. Sections the
        member cannot act on are omitted, never errored — a member whose
        allowlist lacks file_read still gets the injected text."""
        sections = []
        spec_path = os.path.abspath(os.path.join(self.state_dir, "SPEC.md"))
        try:
            with open(spec_path, "r", encoding="utf-8") as fh:
                spec_text = fh.read()
        except OSError:
            spec_text = ""
        if spec_text.strip():
            sections.append(
                "[team spec]\n%s\n(spec path: %s)"
                % (self._truncate_utf8(spec_text, TEAM_SPEC_CAP_BYTES), spec_path)
            )
            logger.info("team spec injected member=%s path=%s", member.name, spec_path)
        if member.context.strip():
            sections.append(
                "[member context]\n%s"
                % self._truncate_utf8(member.context, MEMBER_CONTEXT_CAP_BYTES)
            )
            logger.info("member context injected member=%s", member.name)
        if not sections:
            return objective
        return "\n\n".join(sections) + "\n\n" + objective

    async def run_member(
        self, member: TeamMember, task_id: str, brief: SpawnBrief, agent_fn, depth: int = 1
    ) -> SubagentResult:
        """Enforce the brief contract, stack member constraints (can only
        tighten), run through SubagentRunner; the result summary lands on
        the task board, never directly on the parent. `depth` is the
        caller's global orchestration depth; team-internal max_depth stacks
        on top and the combined total must stay <= 3."""
        self._require_active()
        full_brief = brief if isinstance(brief, SpawnBrief) else brief_from_event(brief)
        if depth < 1 or depth + self.definition.max_depth > 3:
            raise TeamStateError(
                "orchestration depth overflow: caller depth %d + team max_depth %d exceeds 3"
                % (depth, self.definition.max_depth)
            )
        budget = min(member.budget_tokens, full_brief.token_budget)
        prompt = self._assemble_member_prompt(member, full_brief.objective)
        spec = SubagentSpec(
            name=member.name,
            prompt=prompt,
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
        attempt = self._next_attempt(task_id)
        stream = self._open_run_stream(task_id, attempt)
        base_payload = {
            "task_id": task_id,
            "member": member.name,
            "team": self.definition.name,
            "attempt": attempt,
        }
        if stream is not None:
            stream.append("run_started", dict(base_payload))
            stream.append("prompt", {**base_payload, "text": prompt})
        self._emit("team.member.started", {"member": member.name, "task_id": task_id})

        def _sink(event_type: str, payload: dict) -> None:
            if stream is not None:
                stream.append(event_type, {**base_payload, **_redact(payload)})

        captured: Dict[str, Any] = {}

        async def _wrapped(spec, session):
            captured["session"] = session
            return await agent_fn(spec, session)

        import asyncio

        steer_queue: asyncio.Queue = asyncio.Queue()
        self._supplement_queues[task_id] = steer_queue
        try:
            result = await self.runner.run(
                spec, _wrapped, event_sink=_sink, steer_queue=steer_queue
            )
        finally:
            self._supplement_queues.pop(task_id, None)
        if stream is not None:
            session = captured.get("session")
            stream.append(
                "final",
                {
                    **base_payload,
                    "status": result.status,
                    "summary": result.summary[:500],
                    "error": result.error,
                },
            )
            stream.append(
                "run_finished",
                {
                    **base_payload,
                    "status": result.status,
                    "usage": self._usage_snapshot(session)
                    if session is not None
                    else {"total_tokens": result.tokens_spent},
                },
            )
            stream.close()
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

    # ---------- mid-run steering (issue #64) ----------

    def steer_member(self, task_id: str, content: str, origin: str = "operator") -> bool:
        """Queue a supplement note for a running member. The daemon-side
        RPC (agent-studio #748) feeds this; harness applies whatever
        arrives — no harness-side cap (daemon owns the 3-per-run cap).
        Returns False when no run is active for the task. Injection into
        the member's context happens only at AgentGraph node boundaries,
        via SubagentSession.drain_steers()."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("steer content required")
        if origin not in ("operator", "lead"):
            raise ValueError("steer origin must be 'operator' or 'lead'")
        queue = self._supplement_queues.get(task_id)
        if queue is None:
            logger.info("steer dropped: no active run task=%s", task_id)
            return False
        prefix = "lead note" if origin == "lead" else "operator note"
        queue.put_nowait("[steer] %s: %s" % (prefix, content.strip()))
        logger.info("steer queued task=%s origin=%s", task_id, origin)
        return True

    # ---------- task wakeup rules (issue #65) ----------

    def _armed_wakeups(self, task: TeamTask, now: float) -> List[Dict[str, Any]]:
        rules = [r for r in task.metadata.get("wakeups", []) if not r.get("fired")]
        return [r for r in rules if r.get("expires_at") is None or r["expires_at"] > now]

    def add_wakeup(self, task_id: str, rule: Dict[str, Any]) -> Dict[str, Any]:
        """Store a task-level wakeup rule in task.metadata["wakeups"].
        v1 validates kind/at/every/expires semantics; `on_event` conditions
        are explicitly phase 2 and rejected here. Cap: 8 armed rules per
        task, enforced at this runtime boundary."""
        self._require_active()
        task = self._require_task(task_id)
        if not isinstance(rule, dict):
            raise TeamStateError("wakeup rule must be a dict")
        if "on_event" in rule:
            raise TeamStateError("on_event wakeup conditions are phase 2 (v1 rejects them)")
        kind = rule.get("kind")
        if kind not in _WAKEUP_KINDS:
            raise TeamStateError("wakeup kind must be 'at' or 'every': %r" % (kind,))
        if kind == "at" and not isinstance(rule.get("at"), (int, float)):
            raise TeamStateError("wakeup kind 'at' requires a numeric 'at' epoch")
        if kind == "every" and (
            not isinstance(rule.get("every"), (int, float)) or rule["every"] <= 0
        ):
            raise TeamStateError("wakeup kind 'every' requires a positive numeric 'every'")
        action = rule.get("on_timeout", "wake")
        if action not in _WAKEUP_TIMEOUT_ACTIONS:
            raise TeamStateError("on_timeout must be 'wake' or 'end': %r" % (action,))
        expires_at = rule.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise TeamStateError("expires_at must be numeric or absent")
        now = self._clock()
        if len(self._armed_wakeups(task, now)) >= WAKEUP_CAP_PER_TASK:
            raise TeamStateError(
                "wakeup cap reached: %d armed rules per task (task=%s)"
                % (WAKEUP_CAP_PER_TASK, task_id)
            )
        seq = task.metadata.setdefault("wakeup_seq", 0) + 1
        task.metadata["wakeup_seq"] = seq
        stored = {
            "wake_id": "wk_%d" % seq,
            "kind": kind,
            "on_timeout": action,
            "expires_at": expires_at,
            "created": now,
            "fired": False,
            "last_fired": None,
        }
        if kind == "at":
            stored["at"] = float(rule["at"])
        else:
            stored["every"] = float(rule["every"])
        task.metadata.setdefault("wakeups", []).append(stored)
        logger.info("wakeup added task=%s id=%s kind=%s", task_id, stored["wake_id"], kind)
        self._emit(
            "team.wakeup.added", {"task_id": task_id, "wake_id": stored["wake_id"], "kind": kind}
        )
        return dict(stored)

    def check_wakeups(self, task_id: str, now: float = None) -> List[Dict[str, Any]]:
        """Fire due wakeups for a task: an `at` rule fires exactly once; an
        `every` rule refires on its interval. `wake` action moves a blocked
        task back to queued (the lead's pick pool); `end` action cancels
        with reason wakeup_timeout. Returns the fired-event list."""
        self._require_active()
        task = self._require_task(task_id)
        now = self._clock() if now is None else now
        fired = []
        for rule in list(task.metadata.get("wakeups", [])):
            if rule.get("fired") or not self._wakeup_due(rule, now):
                continue
            action = rule.get("on_timeout", "wake")
            if action == "end" and task.status == "blocked":
                self.transition_task(task_id, "cancelled", reason="wakeup_timeout")
            elif action == "wake" and task.status == "blocked":
                self.transition_task(task_id, "queued")
            if rule["kind"] == "at":
                rule["fired"] = True
            rule["last_fired"] = now
            fired.append({"wake_id": rule["wake_id"], "action": action})
            logger.info(
                "wakeup fired task=%s id=%s action=%s status=%s",
                task_id,
                rule["wake_id"],
                action,
                task.status,
            )
        if fired:
            self._emit("team.wakeup.fired", {"task_id": task_id, "fired": fired})
        return fired

    @staticmethod
    def _wakeup_due(rule: Dict[str, Any], now: float) -> bool:
        if rule.get("expires_at") is not None and rule["expires_at"] <= now:
            return False
        if rule["kind"] == "at":
            return now >= rule["at"]
        anchor = rule.get("last_fired") if rule.get("last_fired") is not None else rule["created"]
        return now >= anchor + rule["every"]

    # ---------- mailbox ----------

    def _inbox_path(self, member_name: str) -> str:
        return os.path.join(self.state_dir, "inboxes", "%s.json" % member_name)

    def _require_member(self, name: str) -> None:
        try:
            self.definition.member(name)
        except TeamDefError as exc:
            raise TeamStateError(str(exc))

    def send(self, msg: TeamMessage) -> Dict[str, Any]:
        """Delivered only when the inbox file write succeeds. Returns
        {"delivered": bool, "msg_id": str} — msg_id is generated here and
        stored in the inbox record."""
        self._require_member(msg.sender)
        self._require_member(msg.recipient)
        for name in msg.mentions:
            self._require_member(name)
        if not msg.msg_id:
            msg.msg_id = "msg_%s" % uuid.uuid4().hex[:8]
        record = json.loads(json.dumps(asdict(msg), ensure_ascii=False))
        self._inboxes[msg.recipient].append(record)
        if not self._persist_inbox(msg.recipient):
            self._inboxes[msg.recipient].pop()
            logger.error("mailbox delivery failed recipient=%s", msg.recipient)
            return {"delivered": False, "msg_id": msg.msg_id}
        self._emit(
            "team.msg.sent",
            {"from": msg.sender, "to": msg.recipient, "type": msg.type, "msg_id": msg.msg_id},
        )
        return {"delivered": True, "msg_id": msg.msg_id}

    def broadcast(self, sender: str, body: str, type_: str = "text", mentions: tuple = ()) -> int:
        """One delivery per member (sender excluded). Mentions validated
        against the member list up front — unknown mention rejects the whole
        broadcast before anything is queued."""
        self._require_member(sender)
        for name in mentions:
            self._require_member(name)
        delivered = 0
        for member in self.definition.members:
            if member.name == sender:
                continue
            outcome = self.send(
                TeamMessage(
                    type=type_, sender=sender, recipient=member.name, body=body, mentions=mentions
                )
            )
            if outcome["delivered"]:
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
