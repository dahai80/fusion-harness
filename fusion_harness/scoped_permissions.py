"""Scoped permission engine (issue #34, K1, ported from cowork
permission_manager.py 5-level semantics).

Deny-rules-first, scope-prefixed rules (file:/command:/url:), guard
delegation for high-risk actions, durable approval queue over
CheckpointStore. Additive: fusion_harness/permissions.py API untouched.
Kernel differences from upstream (each intentional):
- subject + expires_at + multi-scope rules are new (upstream has none);
- file: scope matching adds a path-boundary check (upstream raw startswith
  matched file:/a against file:/ab — a known quirk, not copied);
- guard clients/capability gates stay consumer-side — the engine owns only
  the delegation hook and fail-closed queuing;
- upstream hook audit + HITL gate-tool exemptions are consumer wiring, not
  kernel mechanism.
Zero model calls.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from fusion_harness.checkpoint import CheckpointState, CheckpointStore

logger = logging.getLogger(__name__)

HIGH_RISK_NODES = frozenset(
    {
        "shell_exec",
        "python_repl",
        "apply_edit",
        "file_delete",
        "file_copy",
        "file_move",
        "disk_cleaner",
        "desktop_clean",
        "download_organizer",
        "app_lifecycle",
        "screen_capture",
        "clipboard",
        "notification",
        "mouse_click",
        "mouse_move",
        "keyboard_type",
        "keyboard_shortcut",
        "computer_use_loop",
        "browser_automate",
        "cdp_evaluate",
        "cdp_navigate",
        "cdp_screenshot",
        "cdp_click",
        "cdp_fill",
        "cdp_fill_form",
        "cdp_evaluate",
        "cdp_network",
        "cdp_wait_for",
        "managed_browser",
        "fetch_url",
        "http_request",
        "approval_gate",
        "agent_exec",
        "deploy_service",
        "team_call",
        "workflow_ladder",
    }
)

DECISIONS = ("allow", "deny", "ask")

_QUEUE_SESSION = "scoped-permissions"
_QUEUE_KEY = "pending_approvals"


class PermissionLevel(Enum):
    CONFIRM = "confirm"
    MANUAL = "manual"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


class ScopedPermissionError(ValueError):
    """Invalid scoped permission rule or queue operation."""


@dataclass
class ScopedPermission:
    subject: str
    level: str = "confirm"
    scopes: Tuple[str, ...] = ("*",)
    allowed: bool = True
    expires_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.subject:
            raise ScopedPermissionError("subject required")
        if isinstance(self.level, PermissionLevel):
            self.level = self.level.value
        if self.level not in tuple(lv.value for lv in PermissionLevel):
            raise ScopedPermissionError("unknown permission level: %s" % self.level)
        if isinstance(self.scopes, str):
            self.scopes = (self.scopes,)
        if not self.scopes:
            raise ScopedPermissionError("at least one scope required")

    def live(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at

    def matches(self, action: str, resource: str = "", now: float = 0.0) -> bool:
        if not self.live(now):
            return False
        if self.subject != "*" and self.subject != action:
            return False
        return any(_scope_match(scope, resource) for scope in self.scopes)


def _scope_match(scope: str, resource: str) -> bool:
    if scope == "*":
        return True
    if not resource:
        return True
    if scope.startswith("file:"):
        prefix = scope[5:].replace("**", "")
        return _path_match(resource, prefix)
    if scope.startswith("command:"):
        prefix = scope[8:].replace("*", "")
        return resource.startswith(prefix)
    if scope.startswith("url:"):
        prefix = scope[4:].replace("**", "").rstrip("/")
        return resource.rstrip("/") == prefix or resource.rstrip("/").startswith(prefix + "/")
    # plain scope (no file:/command:/url: prefix): prefix match on resource
    # (upstream fell through to True for unknown prefixes — not copied:
    # a scoped deny that matches everything is a deny-first hazard)
    return resource.startswith(scope)


def _path_match(resource: str, prefix: str) -> bool:
    if resource == prefix or resource == prefix.rstrip("/"):
        return True
    boundary = prefix if prefix.endswith("/") else prefix + "/"
    return resource.startswith(boundary)


@dataclass
class PendingApproval:
    id: str
    action: str
    resource: str
    reason: str
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingApproval":
        return cls(
            id=str(data.get("id") or ""),
            action=str(data.get("action") or ""),
            resource=str(data.get("resource") or ""),
            reason=str(data.get("reason") or ""),
            ts=float(data.get("ts") or 0.0),
        )


class ScopedPermissionEngine:
    """Deny-rules-first evaluation over scope-prefixed rules.

    check() order: expired rules skipped -> explicit deny (any level) ->
    engine-level BYPASS -> explicit allow -> high-risk guard delegation
    (no guard or guard deny = fail-closed deny + queued approval) ->
    default allow for low-risk actions.
    """

    def __init__(
        self,
        level: str = "confirm",
        checkpoint_store: Optional[CheckpointStore] = None,
        clock: Optional[Callable[[], float]] = None,
        high_risk: Optional[frozenset] = None,
    ) -> None:
        self.level = level.value if isinstance(level, PermissionLevel) else str(level)
        self.store = checkpoint_store
        self._clock = clock or time.time
        self.high_risk = high_risk if high_risk is not None else HIGH_RISK_NODES
        self._lock = threading.Lock()
        self._rules: List[ScopedPermission] = []
        self._pending: Dict[str, PendingApproval] = {}
        self._guards: Dict[str, Callable[[str, str], Any]] = {}
        self._load_queue()

    # ---------- rules ----------

    def grant(self, permission: ScopedPermission) -> None:
        self._rules_append(permission)

    def _rules_append(self, permission: ScopedPermission) -> None:
        with self._lock:
            self._rules.append(permission)
        logger.info(
            "scoped permission granted subject=%s allowed=%s scopes=%s",
            permission.subject,
            permission.allowed,
            permission.scopes,
        )

    def revoke(self, subject: str) -> int:
        removed = 0
        with self._lock:
            kept = []
            for rule in self._rules:
                if rule.subject == subject:
                    removed += 1
                else:
                    kept.append(rule)
            self._rules = kept
        logger.info("scoped permission revoked subject=%s removed=%d", subject, removed)
        return removed

    def rules(self) -> List[ScopedPermission]:
        with self._lock:
            return list(self._rules)

    # ---------- evaluation ----------

    def check(self, action: str, resource: str = "", now: Optional[float] = None) -> str:
        moment = self._clock() if now is None else now
        with self._lock:
            snapshot = list(self._rules)
        for rule in snapshot:
            if rule.allowed is False and rule.matches(action, resource, moment):
                logger.info("scoped deny subject=%s action=%s", rule.subject, action)
                return "deny"
        if self.level == "bypass":
            return "allow"
        for rule in snapshot:
            if rule.allowed and rule.matches(action, resource, moment):
                return "allow"
        if action in self.high_risk:
            return self._high_risk_path(action, resource)
        return "allow"

    def _high_risk_path(self, action: str, resource: str) -> str:
        guard = self._guards.get(action) or self._guards.get("*")
        if guard is None:
            logger.info("high-risk action without guard, fail-closed action=%s", action)
            self.pending(action, resource, "high-risk without guard (fail-closed)")
            return "deny"
        try:
            verdict = guard(action, resource)
        except Exception as exc:
            logger.warning("guard %s raised: %s — fail-closed", action, exc)
            self.pending(action, resource, "guard error: %s" % exc)
            return "deny"
        if verdict == "allow":
            return "allow"
        if verdict == "ask":
            self.pending(action, resource, "guard requested approval")
            return "ask"
        logger.info("guard denied action=%s", action)
        self.pending(action, resource, "guard denied")
        return "deny"

    def register_guard(self, node_class: str, guard_fn: Callable[[str, str], Any]) -> None:
        """Delegate high-risk verdicts for node_class ("*" = catch-all).
        The engine owns no guard logic — guard_fn(action, resource) returns
        "allow" / "deny" / "ask" (anything else counts as deny)."""
        if not callable(guard_fn):
            raise ScopedPermissionError("guard_fn must be callable")
        self._guards[node_class] = guard_fn
        logger.info("guard registered node_class=%s", node_class)

    # ---------- approval queue (durable) ----------

    def pending(self, action: str, resource: str, reason: str = "") -> str:
        item = PendingApproval(
            id="perm_%s" % uuid.uuid4().hex[:8],
            action=action,
            resource=resource,
            reason=reason,
            ts=self._clock(),
        )
        with self._lock:
            self._pending[item.id] = item
        self._save_queue()
        logger.info("approval queued id=%s action=%s reason=%s", item.id, action, reason)
        return item.id

    def resolve(self, approval_id: str, approve: bool) -> bool:
        with self._lock:
            item = self._pending.pop(approval_id, None)
        if item is None:
            logger.info("resolve unknown approval id=%s", approval_id)
            return False
        rule = ScopedPermission(
            subject=item.action,
            scopes=(item.resource,) if item.resource else ("*",),
            allowed=bool(approve),
        )
        self._rules_append(rule)
        self._save_queue()
        logger.info("approval resolved id=%s approve=%s", approval_id, approve)
        return True

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self._pending.values()]

    def _save_queue(self) -> None:
        if self.store is None:
            return
        with self._lock:
            payload = [item.to_dict() for item in self._pending.values()]
        snap = CheckpointState(messages=[], permission_mode="scoped_permissions")
        snap.metadata = {_QUEUE_KEY: payload}
        self.store.save(_QUEUE_SESSION, snap)

    def _load_queue(self) -> None:
        if self.store is None:
            return
        snap = self.store.latest(_QUEUE_SESSION)
        if snap is None:
            return
        raw = snap.metadata.get(_QUEUE_KEY) or []
        for entry in raw:
            try:
                item = PendingApproval.from_dict(entry)
            except Exception as exc:
                logger.warning("skipping corrupt queued approval: %s", exc)
                continue
            if item.id:
                self._pending[item.id] = item
        logger.info("approval queue restored count=%d", len(self._pending))


__all__ = [
    "DECISIONS",
    "HIGH_RISK_NODES",
    "PermissionLevel",
    "PendingApproval",
    "ScopedPermission",
    "ScopedPermissionEngine",
    "ScopedPermissionError",
]
