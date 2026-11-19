"""Issue #98: policy-as-artifact — DSL validation, interpreter, deny-wins
composition, artifact round-trip, checkpoint carry, adversarial cases."""

import pytest

from fusion_harness import (
    ArtifactStore,
    CheckpointState,
    PolicyEngine,
    PolicyError,
    attach_policy_to_checkpoint,
    policy_diff,
    policy_from_artifact,
    validate_policy,
)


def base_policy(**overrides):
    doc = {
        "version": 1,
        "name": "readonly-refactor",
        "rules": [
            {"effect": "allow", "tools": ["read_*", "grep*"], "constraints": []},
            {
                "effect": "deny",
                "tools": ["shell_exec"],
                "constraints": [{"arg": "command", "op": "not_contains", "value": "rm "}],
            },
            {
                "effect": "ask",
                "tools": ["apply_edit"],
                "constraints": [{"arg": "path", "op": "startswith", "value": "src/"}],
            },
        ],
        "fallback": "ask",
    }
    doc.update(overrides)
    return doc


def test_validate_ok():
    out = validate_policy(base_policy())
    assert out["name"] == "readonly-refactor"
    assert out["fallback"] == "ask"


def test_validate_rejects_bad_version_and_effect():
    with pytest.raises(PolicyError):
        validate_policy(base_policy(version=2))
    with pytest.raises(PolicyError):
        validate_policy(
            base_policy(
                rules=[
                    {"effect": "sudo", "tools": ["x"]},
                ]
            )
        )


def test_validate_rejects_empty_tool_globs():
    with pytest.raises(PolicyError):
        validate_policy(base_policy(rules=[{"effect": "allow", "tools": [], "constraints": []}]))


def test_validate_rejects_unknown_op_and_missing_value():
    with pytest.raises(PolicyError):
        validate_policy(
            base_policy(
                rules=[
                    {
                        "effect": "deny",
                        "tools": ["x"],
                        "constraints": [{"arg": "a", "op": "regex", "value": "."}],
                    }
                ]
            )
        )
    with pytest.raises(PolicyError):
        validate_policy(
            base_policy(
                rules=[
                    {"effect": "deny", "tools": ["x"], "constraints": [{"arg": "a", "op": "eq"}]}
                ]
            )
        )


def test_validate_rejects_oversized():
    doc = base_policy(rules=[{"effect": "allow", "tools": ["x" * 200], "constraints": []}])
    with pytest.raises(PolicyError):
        validate_policy(doc)
    many = [{"effect": "allow", "tools": ["t%d" % i], "constraints": []} for i in range(200)]
    with pytest.raises(PolicyError):
        validate_policy(base_policy(rules=many))


def test_evaluate_allow_ask_deny():
    engine = PolicyEngine([base_policy()])
    assert engine.evaluate("read_file", {"path": "a"})[0] == "allow"
    assert engine.evaluate("apply_edit", {"path": "src/main.py"})[0] == "ask"
    assert engine.evaluate("shell_exec", {"command": "ls"})[0] == "deny"


def test_constraint_omission_cannot_reach_allow():
    # Missing arg -> constraint False -> the lenient deny exception and any
    # allow rule with the same guard both stop matching; the call lands on
    # the fallback (ask), never on allow.
    engine = PolicyEngine([base_policy()])
    decision, _ = engine.evaluate("shell_exec", {})
    assert decision == "ask"
    assert engine.stats["fallbacks"] == 1


def test_fallback_default_ask():
    doc = base_policy()
    doc["rules"] = [{"effect": "allow", "tools": ["read_*"], "constraints": []}]
    doc.pop("fallback", None)
    engine = PolicyEngine([doc])
    assert engine.evaluate("write_file", {"path": "x"})[0] == "ask"
    assert engine.stats["fallbacks"] == 1


def test_stack_deny_wins_never_widens():
    permissive = {
        "version": 1,
        "name": "p",
        "rules": [{"effect": "allow", "tools": ["*"], "constraints": []}],
        "fallback": "allow",
    }
    restrictive = {
        "version": 1,
        "name": "r",
        "rules": [{"effect": "deny", "tools": ["write_file"], "constraints": []}],
        "fallback": "ask",
    }
    engine = PolicyEngine([permissive, restrictive])
    assert engine.evaluate("write_file", {})[0] == "deny"
    assert engine.evaluate("read_file", {})[0] == "allow"
    engine.combine("allow", "deny") == "deny"
    engine.combine("deny", "allow") == "deny"
    engine.combine("ask", "allow") == "ask"


def test_matches_glob_is_fnmatch_not_regex():
    doc = base_policy(rules=[{"effect": "deny", "tools": ["net_[a-z]"], "constraints": []}])
    engine = PolicyEngine([doc])
    assert engine.evaluate("net_a", {})[0] == "deny"
    assert engine.evaluate("net_zz", {})[0] == "ask"


def test_constraint_contradiction_deterministic():
    doc = base_policy(
        rules=[
            {
                "effect": "allow",
                "tools": ["t"],
                "constraints": [
                    {"arg": "a", "op": "contains", "value": "x"},
                    {"arg": "a", "op": "not_contains", "value": "x"},
                ],
            }
        ]
    )
    engine = PolicyEngine([doc])
    assert engine.evaluate("t", {"a": "x"})[0] == "ask"


def test_artifact_roundtrip_and_escape_guard(tmp_path):
    store = ArtifactStore(str(tmp_path))
    import json

    ref = store.write_artifact("run1", "policy", json.dumps(base_policy()), fmt="json")
    doc = policy_from_artifact(store, ref)
    assert doc["name"] == "readonly-refactor"
    with pytest.raises(PolicyError):
        policy_from_artifact(store, "../outside.json")
    with pytest.raises(PolicyError):
        policy_from_artifact(store, "run1/missing.json")


def test_checkpoint_active_policies_survive_metadata_copy(tmp_path):
    from fusion_harness import CheckpointStore

    store = CheckpointStore(str(tmp_path / "cp.db"))
    state = CheckpointState(messages=[], permission_mode="default")
    attach_policy_to_checkpoint(state, "run1/policy.json")
    attach_policy_to_checkpoint(state, "run1/policy.json")
    assert state.metadata["active_policies"] == ["run1/policy.json"]
    seq = store.save("s1", state)
    loaded = store.load("s1", seq)
    assert "run1/policy.json" in loaded.metadata["active_policies"]
    # fork() copies metadata verbatim, so a child lineage inherits exactly
    # the policy set it was spawned with.
    forked = store.fork("s1", seq)
    child = store.load(forked["session_id"], 1)
    assert child.metadata["active_policies"] == ["run1/policy.json"]


def test_policy_diff():
    a = base_policy()
    b = base_policy()
    b["rules"] = a["rules"] + [{"effect": "deny", "tools": ["net_*"], "constraints": []}]
    diff = policy_diff(a, b)
    assert len(diff["added"]) == 1
    assert diff["removed"] == []
    assert diff["unchanged_count"] == 3
