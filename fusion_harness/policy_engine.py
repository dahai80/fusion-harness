"""Declarative policy-as-artifact engine (issue #98, Progent).

Design: docs/design/policy-as-artifact-98.md. Policies are versioned
JSON artifacts (fixed predicate ops, no eval, no regex); the interpreter
is pure and deterministic. Composition is deny-wins intersection: a
policy allow can never widen a permission deny — the host applies the
policy verdict ON TOP of the permission engine's decision.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

POLICY_VERSION = 1
EFFECTS = ("allow", "deny", "ask")
FALLBACKS = ("allow", "deny", "ask")
ALLOWED_OPS = (
    "eq",
    "neq",
    "contains",
    "not_contains",
    "startswith",
    "matches_glob",
    "in",
    "lt",
    "gt",
)

MAX_POLICY_BYTES = 65536
MAX_RULES = 128
MAX_CONSTRAINTS = 32
MAX_TOOL_PATTERN_LEN = 128
MAX_ARG_VALUE_LEN = 4096


class PolicyError(ValueError):
    """Invalid policy document — loud, never silently repaired."""


def validate_policy(doc: Any) -> dict:
    """Validate a policy document; returns it normalized or raises."""
    if not isinstance(doc, dict):
        raise PolicyError("policy must be a JSON object")
    if doc.get("version") != POLICY_VERSION:
        raise PolicyError("unsupported policy version: %r" % (doc.get("version"),))
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        raise PolicyError("policy name must be a non-empty string")
    rules = doc.get("rules")
    if not isinstance(rules, list):
        raise PolicyError("rules must be a list")
    if len(rules) > MAX_RULES:
        raise PolicyError("too many rules: %d > %d" % (len(rules), MAX_RULES))
    for i, rule in enumerate(rules):
        _validate_rule(i, rule)
    fallback = doc.get("fallback", "ask")
    if fallback not in FALLBACKS:
        raise PolicyError("invalid fallback: %r" % (fallback,))
    return {"version": POLICY_VERSION, "name": name, "rules": rules, "fallback": fallback}


def _validate_rule(i: int, rule: Any) -> None:
    if not isinstance(rule, dict):
        raise PolicyError("rule %d must be an object" % i)
    if rule.get("effect") not in EFFECTS:
        raise PolicyError("rule %d has invalid effect: %r" % (i, rule.get("effect")))
    tools = rule.get("tools")
    if not isinstance(tools, list) or not tools:
        raise PolicyError(
            "rule %d needs a non-empty tools list (empty glob = match-nothing is banned)" % i
        )
    for pattern in tools:
        if not isinstance(pattern, str) or not pattern:
            raise PolicyError("rule %d tool patterns must be non-empty strings" % i)
        if len(pattern) > MAX_TOOL_PATTERN_LEN:
            raise PolicyError("rule %d tool pattern too long" % i)
    constraints = rule.get("constraints", [])
    if not isinstance(constraints, list):
        raise PolicyError("rule %d constraints must be a list" % i)
    if len(constraints) > MAX_CONSTRAINTS:
        raise PolicyError("rule %d too many constraints" % i)
    for c in constraints:
        _validate_constraint(i, c)


def _validate_constraint(i: int, c: Any) -> None:
    if not isinstance(c, dict):
        raise PolicyError("rule %d constraint must be an object" % i)
    arg = c.get("arg")
    if not isinstance(arg, str) or not arg:
        raise PolicyError("rule %d constraint needs a non-empty arg name" % i)
    op = c.get("op")
    if op not in ALLOWED_OPS:
        raise PolicyError("rule %d constraint op not allowed: %r" % (i, op))
    if "value" not in c:
        raise PolicyError("rule %d constraint needs a value" % i)
    if op == "in" and not isinstance(c["value"], list):
        raise PolicyError("rule %d 'in' constraint value must be a list" % i)
    if op in ("lt", "gt") and not isinstance(c["value"], (int, float)):
        raise PolicyError("rule %d numeric constraint value must be a number" % i)
    if isinstance(c["value"], str) and len(c["value"]) > MAX_ARG_VALUE_LEN:
        raise PolicyError("rule %d constraint value too long" % i)


def _matches_constraint(c: dict, args: dict) -> bool:
    if c["arg"] not in args:
        # Missing argument: constraint never matches (fail-safe — a
        # not_contains guard cannot be bypassed by omitting the arg).
        return False
    value = args[c["arg"]]
    expected = c["value"]
    op = c["op"]
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, default=str)
    if len(value) > MAX_ARG_VALUE_LEN:
        value = value[:MAX_ARG_VALUE_LEN]
    try:
        if op == "eq":
            return args[c["arg"]] == expected
        if op == "neq":
            return args[c["arg"]] != expected
        if op == "contains":
            return str(expected) in value
        if op == "not_contains":
            return str(expected) not in value
        if op == "startswith":
            return value.startswith(str(expected))
        if op == "matches_glob":
            return fnmatch.fnmatchcase(value, str(expected))
        if op == "in":
            return args[c["arg"]] in expected
        if op == "lt":
            return isinstance(args[c["arg"]], (int, float)) and args[c["arg"]] < expected
        if op == "gt":
            return isinstance(args[c["arg"]], (int, float)) and args[c["arg"]] > expected
    except Exception as e:
        logger.warning("policy constraint evaluation failed op=%s err=%s", op, e)
        return False
    return False


def _rule_matches(rule: dict, tool_name: str, args: dict) -> bool:
    if not any(fnmatch.fnmatchcase(tool_name, p) for p in rule["tools"]):
        return False
    return all(_matches_constraint(c, args) for c in rule.get("constraints", []))


class PolicyEngine:
    """Evaluates a stack of validated policies, deny-wins.

    Evaluation order within a policy: deny -> ask -> allow -> fallback.
    Across a stack: the most restrictive verdict wins (deny > ask >
    allow), so a stack of policies can only narrow.
    """

    def __init__(self, policies: list[dict]):
        self.policies = [validate_policy(p) for p in policies]
        self.stats = {"evaluations": 0, "denies": 0, "asks": 0, "allows": 0, "fallbacks": 0}

    def evaluate(self, tool_name: str, args: dict | None = None) -> tuple[str, str]:
        """Returns (decision, rule_source).

        Explicit rule matches narrow across the whole stack (deny > ask >
        allow). If NO policy matches any rule, the FIRST policy's fallback
        applies (stack order is host-controlled priority)."""
        args = args or {}
        self.stats["evaluations"] += 1
        rank = {"allow": 0, "ask": 1, "deny": 2}
        worst = ""
        source = ""
        for policy in self.policies:
            for effect in ("deny", "ask", "allow"):
                for idx, rule in enumerate(policy["rules"]):
                    if rule["effect"] == effect and _rule_matches(rule, tool_name, args):
                        if not worst or rank[effect] > rank[worst]:
                            worst = effect
                            source = "%s:rule[%d]" % (policy["name"], idx)
                        break
                if worst and rank[worst] == rank[effect]:
                    break
            if worst == "deny":
                break
        if not worst:
            worst = self.policies[0]["fallback"]
            source = "%s:fallback" % self.policies[0]["name"]
            self.stats["fallbacks"] += 1
        self.stats[{"allow": "allows", "ask": "asks", "deny": "denies"}[worst]] += 1
        return worst, source

    def combine(self, permission_decision: str, policy_decision: str) -> str:
        """Intersection with the permission engine — never widens."""
        rank = {"allow": 0, "ask": 1, "deny": 2}
        return (
            permission_decision
            if rank[permission_decision] >= rank[policy_decision]
            else policy_decision
        )


def policy_from_artifact(store, ref: str) -> dict:
    """Load + validate a policy artifact from an ArtifactStore ref.

    Refs are resolved inside the store root only (path escape = loud
    failure); oversized or non-JSON artifacts fail loud, never degraded.
    """
    import os

    root = os.path.realpath(getattr(store, "root_dir", ""))
    path = os.path.realpath(store.artifact_path(ref))
    if not path.startswith(root + os.sep):
        raise PolicyError("policy artifact ref escapes store root: %r" % ref)
    if not os.path.exists(path):
        raise PolicyError("policy artifact not found: %r" % ref)
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError("policy artifact too large: >%d bytes" % MAX_POLICY_BYTES)
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise PolicyError("policy artifact is not valid JSON: %s" % e)
    return validate_policy(doc)


def attach_policy_to_checkpoint(state, ref: str) -> None:
    """Record an active policy ref on a checkpoint state's metadata."""
    active = list(state.metadata.get("active_policies") or [])
    if ref not in active:
        active.append(ref)
    state.metadata["active_policies"] = active
    logger.info("policy attached to checkpoint ref=%s total=%d", ref, len(active))


def policy_diff(doc_a: dict, doc_b: dict) -> dict:
    """Rule-level diff between two validated policies (by canonical JSON)."""
    a = [json.dumps(r, sort_keys=True) for r in validate_policy(doc_a)["rules"]]
    b = [json.dumps(r, sort_keys=True) for r in validate_policy(doc_b)["rules"]]
    added = [r for r in b if r not in a]
    removed = [r for r in a if r not in b]
    return {
        "added": [json.loads(r) for r in added],
        "removed": [json.loads(r) for r in removed],
        "unchanged_count": len(a) - len(removed),
    }
