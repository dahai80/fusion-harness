"""K1 tests (issue #34): scoped permission engine — deny-first, scope
prefix matching (incl. boundary negatives), expiry, guard delegation,
durable approval queue. Behavior cases ported from cowork
test_permission_stage3.py are marked `# cowork:`."""

import pytest

from fusion_harness.checkpoint import CheckpointStore
from fusion_harness.scoped_permissions import (
    DECISIONS,
    HIGH_RISK_NODES,
    PermissionLevel,
    ScopedPermission,
    ScopedPermissionEngine,
    ScopedPermissionError,
)


def _engine(**kw):
    kw.setdefault("clock", lambda: 1000.0)
    return ScopedPermissionEngine(**kw)


class TestRulesAndLevels:
    def test_confirm_is_default(self):  # cowork: test_confirm_is_default
        assert _engine().level == "confirm"
        assert PermissionLevel.CONFIRM.value == "confirm"

    def test_high_risk_denied_without_approve(
        self,
    ):  # cowork: test_high_risk_denied_without_approve
        assert _engine().check("shell_exec") == "deny"

    def test_low_risk_allowed_without_rule(self):  # cowork: test_low_risk_allowed_without_rule
        assert _engine().check("file_input") == "allow"

    def test_allow_rule_allows_high_risk(self):  # cowork: test_approve_rule_allows_high_risk
        eng = _engine()
        eng.grant(ScopedPermission(subject="shell_exec", allowed=True))
        assert eng.check("shell_exec") == "allow"

    def test_deny_rule_rejects_low_risk(self):  # cowork: test_deny_rule_rejects_low_risk
        eng = _engine()
        eng.grant(ScopedPermission(subject="file_input", allowed=False))
        assert eng.check("file_input") == "deny"

    def test_deny_beats_allow_regardless_of_order(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="shell_exec", allowed=True))
        eng.grant(ScopedPermission(subject="shell_exec", allowed=False))
        assert eng.check("shell_exec") == "deny"

    def test_allow_rule_beats_high_risk(
        self,
    ):  # cowork: test_approve_takes_precedence_over_high_risk
        eng = _engine(level="manual")
        eng.grant(ScopedPermission(subject="python_repl", allowed=True))
        assert eng.check("python_repl") == "allow"

    def test_bypass_allows_all(self):  # cowork: test_bypass_allows_all
        eng = _engine(level="bypass")
        assert eng.check("shell_exec") == "allow"
        assert eng.check("python_repl") == "allow"

    def test_manual_high_risk_without_allow_denied(
        self,
    ):  # cowork: test_manual_high_risk_without_approve_denied
        assert _engine(level="manual").check("file_delete") == "deny"

    def test_bypass_does_not_beat_deny(self):
        eng = _engine(level="bypass")
        eng.grant(ScopedPermission(subject="shell_exec", allowed=False))
        assert eng.check("shell_exec") == "deny"

    def test_level_enum_import_names(self):
        assert [lv.name for lv in PermissionLevel] == [
            "CONFIRM",
            "MANUAL",
            "AUTO",
            "PLAN",
            "BYPASS",
        ]

    def test_invalid_level_rejected(self):
        with pytest.raises(ScopedPermissionError):
            ScopedPermission(subject="x", level="gossip")

    def test_decisions_contract(self):
        assert DECISIONS == ("allow", "deny", "ask")

    def test_revoke(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="file_input", allowed=False))
        assert eng.revoke("file_input") == 1
        assert eng.check("file_input") == "allow"
        assert eng.revoke("file_input") == 0


class TestScopeMatching:
    def test_star_scope_matches_everything(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="http_request", scopes=("*",)))
        assert eng.check("http_request", "https://x.com/a") == "allow"

    def test_file_scope_prefix(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="read_file", scopes=("file:/home/data",)))
        assert eng.check("read_file", "/home/data/a.txt") == "allow"
        assert eng.check("read_file", "/home/data") == "allow"

    def test_file_scope_boundary_negative(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="read_file", scopes=("file:/home/data",)))
        # out-of-scope resource is NOT matched by the rule; low-risk default allow
        assert eng.check("read_file", "/home/database") == "allow"

    def test_command_scope_prefix(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="shell_exec", scopes=("command:git ",)))
        assert eng.check("shell_exec", "git status") == "allow"
        assert eng.check("shell_exec", "rm -rf /") == "deny"

    def test_url_scope(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="http_request", scopes=("url:http://127.0.0.1:11434",)))
        assert eng.check("http_request", "http://127.0.0.1:11434/api/tags") == "allow"
        assert eng.check("http_request", "http://127.0.0.1:9999/x") == "deny"

    def test_multi_scope_any_match(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="t", scopes=("file:/a", "file:/b")))
        assert eng.check("t", "/b/x") == "allow"
        assert eng.check("t", "/c/x") == "allow"  # out of scope, low-risk default

    def test_empty_resource_matches_any_non_star_scope(self):  # cowork: empty params permissive
        eng = _engine()
        eng.grant(ScopedPermission(subject="read_file", scopes=("file:/a",)))
        assert eng.check("read_file") == "allow"

    def test_subject_mismatch_ignored(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="other_tool", allowed=False))
        assert eng.check("read_file") == "allow"

    def test_star_subject_matches_all(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="*", allowed=False, scopes=("file:/secret",)))
        assert eng.check("any_tool", "/secret/x") == "deny"


class TestExpiry:
    def test_expired_allow_ignored(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="shell_exec", allowed=True, expires_at=500.0))
        assert eng.check("shell_exec", now=100.0) == "allow"
        assert eng.check("shell_exec", now=1000.0) == "deny"

    def test_expired_deny_ignored(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="read_file", allowed=False, expires_at=500.0))
        assert eng.check("read_file", now=1000.0) == "allow"

    def test_no_expiry_never_expires(self):
        eng = _engine()
        eng.grant(ScopedPermission(subject="read_file", allowed=True))
        assert eng.check("read_file", now=10**12) == "allow"


class TestGuardDelegation:
    def test_guard_allow(self):
        eng = _engine()
        eng.register_guard("shell_exec", lambda action, res: "allow")
        assert eng.check("shell_exec") == "allow"
        assert eng.list_pending() == []

    def test_guard_deny_queues(self):
        eng = _engine()
        eng.register_guard("shell_exec", lambda action, res: "deny")
        assert eng.check("shell_exec") == "deny"
        pending = eng.list_pending()
        assert len(pending) == 1
        assert pending[0]["action"] == "shell_exec"

    def test_no_guard_fail_closed_queues(self):
        eng = _engine()
        assert eng.check("shell_exec") == "deny"
        assert len(eng.list_pending()) == 1

    def test_guard_ask_returns_ask(self):
        eng = _engine()
        eng.register_guard("shell_exec", lambda action, res: "ask")
        assert eng.check("shell_exec") == "ask"

    def test_guard_exception_fail_closed(self):
        def boom(action, res):
            raise RuntimeError("guard down")

        eng = _engine()
        eng.register_guard("shell_exec", boom)
        assert eng.check("shell_exec") == "deny"
        assert len(eng.list_pending()) == 1

    def test_catchall_guard(self):
        eng = _engine()
        eng.register_guard("*", lambda action, res: "allow")
        assert eng.check("deploy_service") == "allow"

    def test_high_risk_names_present(self):  # cowork: test_all_expected_high_risk_present
        expected = {
            "shell_exec",
            "python_repl",
            "file_delete",
            "http_request",
            "approval_gate",
            "deploy_service",
        }
        assert expected.issubset(HIGH_RISK_NODES)

    def test_low_risk_not_in_high_risk(self):  # cowork: test_low_risk_not_in_high_risk
        assert HIGH_RISK_NODES.isdisjoint({"file_input", "file_output", "filter", "merge"})


class TestApprovalQueue:
    def test_resolve_approve_persists_allow_rule(self):
        eng = _engine()
        pid = eng.pending("shell_exec", "command:git ", "guard denied")
        assert eng.resolve(pid, True) is True
        assert eng.check("shell_exec", "git status") == "allow"
        assert eng.list_pending() == []

    def test_resolve_deny_persists_deny_rule(self):
        eng = _engine()
        pid = eng.pending("read_file", "/tmp/x", "suspicious")
        eng.resolve(pid, False)
        assert eng.check("read_file", "/tmp/x") == "deny"
        # deny is scope-limited: other resources fall through to default allow
        assert eng.check("read_file", "/tmp/other") == "allow"

    def test_resolve_unknown_id_false(self):
        assert _engine().resolve("perm_ghost", True) is False

    def test_queue_persistence_roundtrip(self):
        store = CheckpointStore(":memory:")
        eng1 = _engine(checkpoint_store=store)
        pid = eng1.pending("shell_exec", "command:git ", "crash pending")
        eng2 = _engine(checkpoint_store=store)
        restored = eng2.list_pending()
        assert len(restored) == 1
        assert restored[0]["id"] == pid
        assert eng2.resolve(pid, True) is True
        assert eng2.check("shell_exec", "git push") == "allow"

    def test_queue_survives_without_store(self):
        eng = _engine()
        eng.pending("shell_exec", "", "no store")
        assert len(eng.list_pending()) == 1
