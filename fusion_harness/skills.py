"""Skills mechanism + permission derivation (plan P2-3, gap G2).

SKILL.md-style packaged skills: simple key/value frontmatter + markdown body.
From paper 2611.08905, per-subagent tool/workspace authorization is derived
from skill content — deterministic parser, never model-judged (kernel Rule 5).
Derived rules land at the node layer; a skill claiming tools outside its own
grant is rejected at load (fail-closed).
"""

import logging
import re
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

    def names(self) -> List[str]:
        return sorted(self._skills)

    def derived_rules(self, name: str) -> List[Dict[str, Any]]:
        return derive_permissions(self.get(name))


__all__ = [
    "Skill",
    "SkillGrant",
    "SkillError",
    "SkillRegistry",
    "parse_skill",
    "derive_permissions",
]
