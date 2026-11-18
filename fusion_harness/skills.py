"""Skills mechanism + permission derivation (plan P2-3, gap G2).

SKILL.md-style packaged skills: simple key/value frontmatter + markdown body.
From paper 2611.08905, per-subagent tool/workspace authorization is derived
from skill content — deterministic parser, never model-judged (kernel Rule 5).
Derived rules land at the node layer; a skill claiming tools outside its own
grant is rejected at load (fail-closed).
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class SkillGrant:
    allow_tools: List[str] = field(default_factory=list)
    deny_tools: List[str] = field(default_factory=list)
    workspace_paths: List[str] = field(default_factory=list)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    grant: SkillGrant = field(default_factory=SkillGrant)
    source: str = ""

    def render_prompt(self) -> str:
        return f"# Skill: {self.name}\n\n{self.description}\n\n{self.body}".strip()


class SkillError(Exception):
    pass


def parse_skill(text: str, source: str = "") -> Skill:
    """Parse SKILL.md content: `---` frontmatter (name/description/permissions)
    then body. Fail-closed: missing name or malformed permission lines reject
    the skill at load."""
    match = _FRONTMATTER_RE.match(text)
    front: Dict[str, Any] = {}
    body = text
    if match:
        body = text[match.end() :]
        for raw in match.group(1).splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                if line:
                    raise SkillError("malformed frontmatter line in %s: %r" % (source, line))
                continue
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
    name = str(front.get("name") or "").strip()
    if not name:
        raise SkillError("skill missing name: %s" % (source or "<inline>"))
    description = str(front.get("description") or "").strip()
    grant = _parse_grant(front.get("permissions") or "", source)
    skill = Skill(name=name, description=description, body=body.strip(), grant=grant, source=source)
    logger.info(
        "skill loaded name=%s allow=%d deny=%d paths=%d source=%s",
        name,
        len(grant.allow_tools),
        len(grant.deny_tools),
        len(grant.workspace_paths),
        source or "<inline>",
    )
    return skill


def _parse_grant(raw: str, source: str) -> SkillGrant:
    grant = SkillGrant()
    if not raw:
        return grant
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise SkillError("malformed permission clause in %s: %r" % (source, part))
        key, _, value = part.partition(":")
        key = key.strip().lower()
        values = [v.strip() for v in value.split(",") if v.strip()]
        if key == "allow_tools":
            grant.allow_tools = values
        elif key == "deny_tools":
            grant.deny_tools = values
        elif key == "workspace":
            grant.workspace_paths = values
        else:
            raise SkillError("unknown permission key %r in %s" % (key, source))
    return grant


def derive_permissions(skill: Skill) -> List[Dict[str, Any]]:
    """Derive node-layer permission rule dicts from a skill's grant.

    deny clauses always win: any tool in both allow and deny is deny-only
    (strictest-wins, matching the permission engine's conflict rule).
    Returns plain dicts (engine-shaped) so the kernel stays decoupled from
    the engine's constructor while consumers map them onto PermissionRule.
    """
    rules: List[Dict[str, Any]] = []
    denied = set(skill.grant.deny_tools)
    for tool in skill.grant.allow_tools:
        if tool in denied:
            continue
        rules.append(
            {
                "action": "allow",
                "tool": _glob_to_regex(tool),
                "path_scope": list(skill.grant.workspace_paths),
                "layer": "node",
                "description": "derived from skill %s" % skill.name,
            }
        )
    for tool in skill.grant.deny_tools:
        rules.append(
            {
                "action": "deny",
                "tool": _glob_to_regex(tool),
                "path_scope": [],
                "layer": "node",
                "description": "derived from skill %s" % skill.name,
            }
        )
    logger.info("permissions derived skill=%s rules=%d", skill.name, len(rules))
    return rules


def _glob_to_regex(glob: str) -> str:
    """Tool names are exact or trailing-* globs; convert to anchored regex
    (the permission engine matches rules via re.search on the tool field)."""
    if glob.endswith("*"):
        return "^" + re.escape(glob[:-1]) + ".*$"
    return "^" + re.escape(glob) + "$"


class SkillRegistry:
    def __init__(self, skills: Optional[List[Skill]] = None) -> None:
        self._skills: Dict[str, Skill] = {}
        for s in skills or []:
            self.register(s)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise SkillError("duplicate skill name: %s" % skill.name)
        self._skills[skill.name] = skill
        logger.debug("skill registered name=%s", skill.name)

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError("no such skill: %s" % name)
        return self._skills[name]

    def replace(self, skill: Skill) -> None:
        if skill.name not in self._skills:
            raise SkillError("no such skill to replace: %s" % skill.name)
        self._skills[skill.name] = skill
        logger.debug("skill replaced name=%s", skill.name)

    def names(self) -> List[str]:
        return sorted(self._skills)

    def derived_rules(self, name: str) -> List[Dict[str, Any]]:
        return derive_permissions(self.get(name))


STATUS_ACTIVE = "active"
STATUS_CANARY = "canary"
STATUS_ARCHIVED = "archived"


@dataclass
class SkillVersion:
    version: int
    skill: Skill
    provenance: str
    ts: float
    status: str


@dataclass
class SkillCase:
    must_mention: List[str] = field(default_factory=list)


class SkillLibrary:
    """Skill lifecycle over SkillRegistry (issue #54): four gates on every
    write (generation/verification/canary/rollback — arXiv 2611.15268),
    usage-rate pruning that archives instead of deleting (arXiv 2611.19823),
    and task-characteristic routing to cut retrieval noise (arXiv 2611.17007)."""

    def __init__(
        self, registry: Optional[SkillRegistry] = None, clock: Optional[Any] = None
    ) -> None:
        self.registry = registry or SkillRegistry()
        self._clock = clock or time.time
        self._history: Dict[str, List[SkillVersion]] = {}
        self._status: Dict[str, str] = {}
        self._usage: Dict[str, int] = {}
        self._created: Dict[str, float] = {}

    def propose(
        self,
        skill: Skill,
        provenance: str = "",
        cases: Optional[List[SkillCase]] = None,
        canary_confirmed: bool = False,
        base_version: Optional[int] = None,
    ) -> Skill:
        """Write a new skill or self-modification through the four gates.
        Any failed gate blocks the write (SkillError naming the gate)."""
        previous = self._history.get(skill.name)
        # Gate 1 — generation: provenance recorded.
        if not provenance or not provenance.strip():
            raise SkillError("gate generation: provenance is required")
        # Gate 2 — verification: validated against known cases.
        rendered = skill.render_prompt()
        for case in cases or []:
            for token in case.must_mention:
                if token not in rendered:
                    raise SkillError("gate verification: prompt missing %r" % token)
        # Gate 3 — canary: limited rollout asserted before general availability.
        if not canary_confirmed:
            raise SkillError("gate canary: limited rollout must be confirmed")
        # Gate 4 — rollback: modifications lock on the current version so the
        # previous one stays restorable; stale-base proposals are blocked.
        version = 1
        if previous:
            current = previous[-1].version
            if base_version != current:
                raise SkillError(
                    "gate rollback: base_version %s stale (current %d)" % (base_version, current)
                )
            version = current + 1
        if skill.name in self.registry.names():
            self.registry.replace(skill)
        else:
            self.registry.register(skill)
        self._history.setdefault(skill.name, []).append(
            SkillVersion(
                version=version,
                skill=skill,
                provenance=provenance,
                ts=self._clock(),
                status=STATUS_ACTIVE,
            )
        )
        self._status[skill.name] = STATUS_ACTIVE
        self._usage.setdefault(skill.name, 0)
        self._created.setdefault(skill.name, self._clock())
        logger.info(
            "skill write accepted name=%s version=%d provenance=%s", skill.name, version, provenance
        )
        return skill

    def rollback(self, name: str, version: int) -> Skill:
        history = self._history.get(name) or []
        for entry in history:
            if entry.version == version:
                if name in self.registry.names():
                    self.registry.replace(entry.skill)
                else:
                    self.registry.register(entry.skill)
                self._status[name] = STATUS_ACTIVE
                logger.info("skill rolled back name=%s version=%d", name, version)
                return entry.skill
        raise SkillError("no such version %s for skill %s" % (version, name))

    def record_usage(self, name: str) -> None:
        self._usage[name] = self._usage.get(name, 0) + 1

    def prune(self, min_uses: int, window_s: float) -> List[str]:
        """Archive skills below the usage threshold after the retention
        window — archived skills stay as the audit trail, never deleted,
        and can no longer be selected."""
        now = self._clock()
        archived = []
        for name, uses in list(self._usage.items()):
            if self._status.get(name) == STATUS_ARCHIVED:
                continue
            if uses < min_uses and now - self._created.get(name, now) > window_s:
                self._status[name] = STATUS_ARCHIVED
                archived.append(name)
                logger.info("skill archived by usage pruning name=%s uses=%d", name, uses)
        return archived

    def select(self, task_text: str, top_k: int = 3) -> List[Skill]:
        """Task-characteristic routing: skills scored by token overlap with
        the task's features instead of flat retrieval. Archived skills are
        never selectable."""
        tokens = set(re.findall(r"\w+", (task_text or "").lower()))
        scored = []
        for name in self.registry.names():
            if self._status.get(name) == STATUS_ARCHIVED:
                continue
            skill = self.registry.get(name)
            hay = set(
                re.findall(
                    r"\w+", ("%s %s %s" % (skill.name, skill.description, skill.body)).lower()
                )
            )
            overlap = len(tokens & hay)
            scored.append((overlap, skill.name, skill))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [s for _, _, s in scored[:top_k]]

    def status(self, name: str) -> str:
        return self._status.get(name, "")

    def usage(self, name: str) -> int:
        return self._usage.get(name, 0)


__all__ = [
    "Skill",
    "SkillGrant",
    "SkillError",
    "SkillRegistry",
    "parse_skill",
    "derive_permissions",
    "SkillCase",
    "SkillLibrary",
    "SkillVersion",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "STATUS_CANARY",
]
