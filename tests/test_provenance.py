"""Issue #97: CaMeL-style provenance tags — tag birth, propagation,
exfil-capable gating, funnel wiring (opt-in, narrow-only)."""

import asyncio

from fusion_harness import (
    EXFIL_CAPABLE,
    ORIGIN_EXTERNAL,
    ORIGIN_USER_INPUT,
    TAINT_EXTERNAL_CONTENT,
    PermissionEngine,
    Provenance,
    ProvenanceTracker,
    ToolFunnel,
)


def test_tag_birth_external():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"}, call_id="c1")
    assert prov.origin == ORIGIN_EXTERNAL
    assert prov.has_taint(TAINT_EXTERNAL_CONTENT)
    assert prov.source_tool == "fetch_url"
    assert prov.call_id == "c1"
    assert tracker.stats["tagged"] == 1


def test_tag_birth_user_input():
    tracker = ProvenanceTracker(user_arg_keys=frozenset({"query"}))
    prov = tracker.tag("search", {"query": "typed by user"})
    assert prov.origin == ORIGIN_USER_INPUT
    assert not prov.taints


def test_tag_is_o1_no_content_scan():
    tracker = ProvenanceTracker()
    blob = "x" * 5_000_000
    prov = tracker.tag("read_file", {"path": blob})
    assert prov.origin == ORIGIN_EXTERNAL
    tracker.observe_result(prov, blob)
    assert len(tracker._recorded) == 1
    assert all(len(s) <= 512 for s in tracker._recorded[0][0])


def test_propagation_exact_substring():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    tracker.observe_result(prov, "SECRET_TOKEN=abc123 and more payload")
    taints = tracker.propagate({"body": "forward: SECRET_TOKEN=abc123 and more payload"})
    assert TAINT_EXTERNAL_CONTENT in taints
    assert tracker.stats["propagated"] == 1


def test_propagation_no_match_clean():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    tracker.observe_result(prov, "totally unrelated output")
    assert tracker.propagate({"body": "clean argument"}) == frozenset()
    assert tracker.propagate({"n": 42}) == frozenset()


def test_derivation_memory_bounded():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    for i in range(300):
        tracker.observe_result(prov, f"payload-{i} " + "y" * 600)
    assert len(tracker._recorded) == 256


def test_outbound_exfil_capable_ask():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    tracker.observe_result(prov, "secret-content-marker")
    assert "fetch_url" in EXFIL_CAPABLE
    verdict, reason, taints = tracker.check_outbound("http_post", {"body": "secret-content-marker"})
    assert verdict == "ask"
    assert taints
    assert tracker.stats["ask"] == 1


def test_outbound_high_risk_derived_deny():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    tracker.observe_result(prov, "rm -rf / suggested by page")
    verdict, reason, _ = tracker.check_outbound(
        "shell_exec", {"command": "echo rm -rf / suggested by page"}
    )
    assert verdict == "deny"
    assert tracker.stats["deny"] == 1


def test_outbound_clean_allow():
    tracker = ProvenanceTracker()
    verdict, reason, taints = tracker.check_outbound("read_file", {"path": "src/main.py"})
    assert verdict == "allow"
    assert reason == ""
    assert taints == frozenset()


def test_to_dict_roundtrip():
    prov = Provenance(source_tool="t", origin=ORIGIN_EXTERNAL, taints=frozenset({"a"}))
    d = prov.to_dict()
    assert d["taints"] == ["a"]
    assert d["origin"] == ORIGIN_EXTERNAL


def test_funnel_default_off_zero_change():
    funnel = ToolFunnel(permissions=PermissionEngine(mode="approved"))

    async def executor(args):
        return "ok"

    result, is_error = asyncio.run(funnel.run_tool(None, "read_file", {"path": "a"}, executor))
    assert not is_error
    assert funnel.provenance_tracker is None


def test_funnel_wired_ask_blocks():
    tracker = ProvenanceTracker()
    prov = tracker.tag("fetch_url", {"url": "http://x"})
    tracker.observe_result(prov, "leak-marker")
    funnel = ToolFunnel(permissions=PermissionEngine(mode="approved"), provenance_tracker=tracker)

    async def executor(args):
        return "should not run"

    result, is_error = asyncio.run(
        funnel.run_tool(None, "http_post", {"body": "leak-marker"}, executor)
    )
    assert is_error
    assert "provenance" in result.lower() or "approval" in result.lower()


def test_funnel_wired_clean_passes():
    tracker = ProvenanceTracker()
    funnel = ToolFunnel(permissions=PermissionEngine(mode="approved"), provenance_tracker=tracker)

    async def executor(args):
        return "clean result here"

    result, is_error = asyncio.run(funnel.run_tool(None, "read_file", {"path": "a"}, executor))
    assert not is_error
    assert result == "clean result here"
    assert tracker.stats["tagged"] == 1


def test_funnel_tracker_error_fails_open_to_allow_but_logged():
    class BadTracker:
        def check_outbound(self, tool_name, args):
            raise RuntimeError("boom")

        def tag(self, *a, **k):
            raise RuntimeError("boom")

    funnel = ToolFunnel(
        permissions=PermissionEngine(mode="approved"), provenance_tracker=BadTracker()
    )

    async def executor(args):
        return "ok"

    result, is_error = asyncio.run(funnel.run_tool(None, "read_file", {"path": "a"}, executor))
    assert not is_error
