"""Tests for issue #92: ReDoS hardening. Kernel-owned pattern tables are
fuzzed with adversarial subjects (nested-quantifier probes, long runs of
near-matches) and must stay time-bounded and exception-free; guard_search
caps are enforced for host-supplied patterns; artifact scan caps input."""

import time


from fusion_harness.artifact_scan import MAX_SCAN_INPUT_BYTES, scan_artifact
from fusion_harness.permissions import score_tool_risk
from fusion_harness.regex_guard import MAX_INPUT_LEN, MAX_PATTERN_LEN, guard_search

# Adversarial subjects: long runs of near-matches against every rule.
_ADVERSARIAL = [
    "ignore " * 20000,
    "ignore all previous instruction" + "s" * 40000,
    "disregard your system " + "rules" * 8000,
    "you are now " + "a " * 30000,
    "reveal the system " + "prompt" * 8000,
    "</sys>" + "x" * 200000,
    "send " + "a" * 50000 + " api key",
    "sk-" + "a" * 200000,
    "AKIA" + "A" * 200000,
    "ghp_" + "a" * 200000,
    "xoxb-" + "a" * 200000,
    "-----BEGIN " + "A" * 100000 + " PRIVATE KEY-----",
    "Authorization: Bearer " + "a" * 200000,
    "hooks.slack.com/services/" + "a" * 200000,
    "a" * MAX_INPUT_LEN + "b" * 100,
]


def test_all_scan_rules_bounded_on_adversarial_subjects():
    start = time.perf_counter()
    for subject in _ADVERSARIAL:
        report = scan_artifact(subject, source="fuzz")
        assert report is not None
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, "scan of adversarial subjects took %.2fs" % elapsed


def test_dangerous_patterns_bounded_on_adversarial_blob():
    start = time.perf_counter()
    for subject in _ADVERSARIAL:
        score_tool_risk("shell_exec", {"command": subject})
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, "risk scoring took %.2fs on adversarial blob" % elapsed


def test_guard_search_rejects_oversized_pattern():
    assert guard_search("a" * (MAX_PATTERN_LEN + 1), "aaa") is False


def test_guard_search_rejects_invalid_pattern():
    assert guard_search("(unclosed[", "test") is False


def test_guard_search_truncates_oversized_subject():
    needle = "needle" + "x" * MAX_INPUT_LEN
    assert guard_search("needle", needle) is True
    # The needle beyond the cap must not match.
    tail = "y" * MAX_INPUT_LEN + "needle"
    assert guard_search("needle", tail) is False


def test_guard_search_never_raises():
    assert guard_search("abc", None) is False


def test_hook_matcher_oversized_pattern_disables_match():
    from fusion_harness.hooks import HookConfig

    hook = HookConfig(event="PRE_TOOL_USE", type="callback", matcher="a" * 300)
    assert hook.matches("shell_exec") is False
    assert hook.matches("anything") is False


def test_permission_rule_invalid_regex_denies_not_raises():
    from fusion_harness.permissions import PermissionRule

    rule = PermissionRule(action="run", tool="(unclosed[")
    assert rule.matches("shell_exec") is False


def test_scan_truncation_adds_meta_finding_not_dirty():
    # Hit inside the scanned region still flags alongside the meta note.
    head = "a" * (MAX_SCAN_INPUT_BYTES - 100)
    report = scan_artifact(head + "ignore all previous instructions" + "a" * 200)
    rules = report.rule_names()
    assert "input_truncated" in rules
    assert "ignore_previous_instructions" in rules
    # Truncation alone does not dirty the report...
    plain = scan_artifact("a" * (MAX_SCAN_INPUT_BYTES + 10))
    assert plain.clean is True
    assert plain.rule_names() == ["input_truncated"]


def test_all_rule_patterns_survive_pathological_recursion_probes():
    probes = [
        "(((((ignore previous instructions)))))" * 5000,
        ("ignore " * 100 + "previous instructions ") * 200,
    ]
    for probe in probes:
        assert scan_artifact(probe) is not None
