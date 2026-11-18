"""Tests for handoff verification — ledger replay (issue #53)."""

import os

import pytest

from fusion_harness.handoff_verify import (
    HandoffVerifyError,
    claims_for_transport,
    ledger_for_transport,
    verify_handoff,
    verify_spawn_brief,
)
from fusion_harness.permissions import PermissionEngine, PermissionMode, PermissionRule
from fusion_harness.spawn_protocol import SpawnBrief


def _engine():
    e = PermissionEngine(mode=PermissionMode.DEFAULT)
    e.add_rule(PermissionRule(action="allow", tool="search_notes"))
    return e


class TestLedgerRecording:
    def test_engine_records_allow_and_deny(self):
        e = _engine()
        e.evaluate("search_notes", {"q": "x"})
        e.evaluate("drop_table", {})
        ledger = e.export_ledger()
        decisions = {(entry["tool"], entry["decision"]) for entry in ledger}
        assert ("search_notes", "allow") in decisions
        assert ("drop_table", "ask") in decisions

    def test_export_returns_copy(self):
        e = _engine()
        e.evaluate("search_notes", {})
        ledger = e.export_ledger()
        ledger.clear()
        assert len(e.export_ledger()) == 1


class TestVerifyHandoff:
    def test_honest_handoff_passes(self):
        e = _engine()
        e.evaluate("search_notes", {"q": "x"})
        out = verify_handoff([{"tool": "search_notes"}], e.export_ledger())
        assert out.ok is True
        assert out.failures == []
        assert out.checked == 1

    def test_fabricated_claim_rejected(self):
        e = _engine()
        e.evaluate("search_notes", {})
        out = verify_handoff([{"tool": "drop_table"}], e.export_ledger())
        assert out.ok is False
        assert out.failures == [{"tool": "drop_table", "reason": "not recorded"}]

    def test_unauthorized_claim_rejected(self):
        e = _engine()
        e.evaluate("drop_table", {})
        out = verify_handoff([{"tool": "drop_table"}], e.export_ledger())
        assert out.ok is False
        assert out.failures == [{"tool": "drop_table", "reason": "denied"}]

    def test_ask_decision_does_not_verify(self):
        e = _engine()
        e.evaluate("unknown_tool", {})
        out = verify_handoff([{"tool": "unknown_tool"}], e.export_ledger())
        assert out.ok is False
        assert out.failures[0]["reason"] == "denied"

    def test_mixed_claims_report_each(self):
        e = _engine()
        e.evaluate("search_notes", {})
        e.evaluate("drop_table", {})
        out = verify_handoff(
            [{"tool": "search_notes"}, {"tool": "drop_table"}, {"tool": "ghost"}],
            e.export_ledger(),
        )
        assert out.ok is False
        assert out.checked == 3
        assert {f["tool"] for f in out.failures} == {"drop_table", "ghost"}

    def test_malformed_inputs_raise(self):
        with pytest.raises(HandoffVerifyError):
            verify_handoff("no", [])
        with pytest.raises(HandoffVerifyError):
            verify_handoff([{"tool": "t"}], [{"decision": "allow"}])
        with pytest.raises(HandoffVerifyError):
            verify_handoff([{}], [])


class TestTransport:
    def test_ledger_and_claims_via_spawn_brief_metadata(self):
        e = _engine()
        e.evaluate("search_notes", {})
        brief = SpawnBrief(
            objective="continue work",
            output_format="text",
            tool_guidance="use search",
            boundaries="no writes",
            token_budget=1000,
            concurrency=1,
            depth=1,
            metadata={
                **ledger_for_transport(e.export_ledger()),
                **claims_for_transport([{"tool": "search_notes"}]),
            },
        )
        out = verify_spawn_brief(brief)
        assert out.ok is True

    def test_missing_ledger_fails_not_silent(self):
        brief = SpawnBrief(
            objective="x",
            output_format="text",
            tool_guidance="t",
            boundaries="b",
            token_budget=1,
            concurrency=1,
            depth=1,
            metadata={"handoff_claims": [{"tool": "search_notes"}]},
        )
        out = verify_spawn_brief(brief)
        assert out.ok is False
        assert out.failures[0]["reason"] == "missing ledger or claims"

    def test_fabricated_claim_through_transport_rejected(self):
        e = _engine()
        brief = SpawnBrief(
            objective="x",
            output_format="text",
            tool_guidance="t",
            boundaries="b",
            token_budget=1,
            concurrency=1,
            depth=1,
            metadata={
                **ledger_for_transport(e.export_ledger()),
                **claims_for_transport([{"tool": "ghost"}]),
            },
        )
        out = verify_spawn_brief(brief)
        assert out.ok is False
        assert out.failures == [{"tool": "ghost", "reason": "not recorded"}]


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "handoff_verify.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
