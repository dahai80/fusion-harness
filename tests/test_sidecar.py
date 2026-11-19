import io
import json
import subprocess
import sys

import pytest

from fusion_harness import KERNEL_VERSION
from fusion_harness.sidecar import PROTOCOL_VERSION, Harnessd, error_resp, serve


@pytest.fixture
def harness():
    return Harnessd(kernel_version=KERNEL_VERSION)


def _rpc(h, method, params=None, id=1):
    return h.handle_sync({"id": id, "method": method, "params": params or {}})


def _hello(h, id=1):
    return _rpc(h, "hello", {"protocol_version": PROTOCOL_VERSION}, id=id)


def test_hello_negotiates_and_gates(harness):
    resp = _rpc(harness, "pre_llm")
    assert resp["error"]["code"] == "protocol"
    hello = _hello(harness)
    assert hello["result"]["protocol_version"] == PROTOCOL_VERSION
    assert hello["result"]["kernel_version"] == KERNEL_VERSION
    assert hello["result"]["commit"]
    assert "tool_admit" in hello["result"]["methods"]


def test_hello_version_mismatch(harness):
    resp = _rpc(harness, "hello", {"protocol_version": 99})
    assert resp["error"]["code"] == "protocol"
    assert "mismatch" in resp["error"]["message"]


def test_pre_llm_fail_closed_internal(harness):
    _hello(harness)
    harness._state = object()  # corrupt: exception must become typed error
    resp = _rpc(harness, "pre_llm", {"messages": []})
    assert resp["error"]["code"] == "internal"
    assert "proceed" not in resp or resp.get("proceed") is not True


def test_pre_llm_budget_deny(harness):
    _hello(harness)
    _rpc(harness, "reset", {"limit_tokens": 10}, id=2)
    _rpc(harness, "post_llm", {"usage": {"prompt_tokens": 20, "completion_tokens": 5}}, id=3)
    resp = _rpc(
        harness,
        "pre_llm",
        {"node_label": "n", "messages": [{"role": "user", "content": "x"}]},
        id=4,
    )
    assert resp["result"]["proceed"] is False
    assert resp["result"]["reason"]


def test_post_llm_and_meter(harness):
    _hello(harness)
    _rpc(harness, "post_llm", {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}, id=2)
    meter = _rpc(harness, "meter", id=3)["result"]
    assert meter["calls"] == 1
    assert meter["budget_spent_tokens"] == 10


def test_tool_admit_unapproved_needs_approval(harness):
    _hello(harness)
    resp = _rpc(
        harness,
        "tool_admit",
        {"tool_name": "shell_exec", "args": {"command": "curl https://example.com"}},
        id=2,
    )
    result = resp["result"]
    assert result["allowed"] is False
    assert result["needs_approval"] is True
    assert result["blocked_by"] == "permission"
    assert result["approval_id"]


def test_tool_admit_low_risk_auto_allows(harness):
    _hello(harness)
    resp = _rpc(harness, "tool_admit", {"tool_name": "read_file", "args": {"path": "/tmp/a"}}, id=2)
    assert resp["result"]["allowed"] is True
    assert resp["result"]["needs_approval"] is False


def test_tool_admit_allow_tool_passes(harness):
    _rpc(harness, "reset", {"allow_tools": ["read_file"]}, id=9)
    _hello(harness, id=10)
    resp = _rpc(harness, "tool_admit", {"tool_name": "read_file", "args": {"path": "/tmp/a"}}, id=11)
    assert resp["result"]["allowed"] is True
    assert resp["result"]["needs_approval"] is False


def test_resolve_approve_grants_and_rechecks(harness):
    _hello(harness)
    adm = _rpc(
        harness,
        "tool_admit",
        {"tool_name": "shell_exec", "args": {"command": "curl https://example.com"}},
        id=2,
    )
    approval_id = adm["result"]["approval_id"]
    ok = _rpc(harness, "resolve", {"approval_id": approval_id, "approve": True}, id=3)
    assert ok["result"]["allowed"] is True
    assert ok["result"]["granted_tool"] == "shell_exec"
    # session grant: same tool no longer asks
    again = _rpc(harness, "tool_admit", {"tool_name": "shell_exec", "args": {"command": "ls"}}, id=4)
    assert again["result"]["allowed"] is True
    assert again["result"]["needs_approval"] is False


def test_resolve_deny_then_unknown(harness):
    _hello(harness)
    adm = _rpc(
        harness,
        "tool_admit",
        {"tool_name": "shell_exec", "args": {"command": "curl https://example.com"}},
        id=2,
    )
    approval_id = adm["result"]["approval_id"]
    deny = _rpc(harness, "resolve", {"approval_id": approval_id, "approve": False}, id=3)
    assert deny["result"]["allowed"] is False
    assert deny["result"]["blocked_by"] == "human_gate"
    unknown = _rpc(harness, "resolve", {"approval_id": "ghost", "approve": True}, id=4)
    assert unknown["error"]["code"] == "state"


def test_resolve_unknown_id(harness):
    _hello(harness)
    resp = _rpc(harness, "resolve", {"approval_id": "ghost", "approve": True})
    assert resp["error"]["code"] == "state"


def test_unknown_method_and_error_resp_shape(harness):
    resp = _rpc(harness, "bogus")
    assert resp["error"]["code"] == "protocol"
    e = error_resp("internal", "boom", req_id=7)
    assert e == {"id": 7, "error": {"code": "internal", "message": "boom"}}


def test_serve_loop_and_malformed_json(harness):
    lines = [
        "not json",
        json.dumps({"id": 1, "method": "hello", "params": {"protocol_version": PROTOCOL_VERSION}}),
        json.dumps({"id": 2, "method": "ping"}),
        "",
    ]
    out = io.StringIO()
    rc = serve(harness, stdin=io.StringIO("\n".join(lines)), stdout=out)
    assert rc == 0
    got = [json.loads(x) for x in out.getvalue().splitlines()]
    assert got[0]["error"]["code"] == "protocol"
    assert got[1]["result"]["protocol_version"] == PROTOCOL_VERSION
    assert got[2]["result"]["pong"] is True


def test_conformance_vectors_pass():
    from fusion_harness.sidecar_conformance import run_vectors

    assert run_vectors(harness_factory=Harnessd) == []


def test_cli_conformance_exit_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "fusion_harness.sidecar", "--conformance"],
        capture_output=True,
        text=True,
        cwd="/tmp",
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "PASS" in proc.stdout
