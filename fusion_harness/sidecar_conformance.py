"""Sidecar protocol v1 conformance vectors (issue #110 §7).

Consumers embed these vectors in their client test suites; the kernel
runs them on itself via `harnessd --conformance`. Each vector runs on
a fresh Harnessd so ordering never matters. A vector is a (request,
expect) pair where expect maps response paths to shape specs: "any" |
"str" | "int" | "bool" | "list" | "obj" | literal JSON value.
"""

import logging

from .sidecar import Harnessd

logger = logging.getLogger(__name__)

SIDECAR_VECTORS = [
    {
        "name": "hello_ok",
        "handshake": False,
        "req": {"id": 1, "method": "hello", "params": {"protocol_version": 1}},
        "expect": {
            "result.protocol_version": 1,
            "result.kernel_version": "str",
            "result.methods": "list",
            "result.features": "list",
        },
    },
    {
        "name": "hello_version_mismatch",
        "handshake": False,
        "req": {"id": 2, "method": "hello", "params": {"protocol_version": 99}},
        "expect": {"error.code": "protocol"},
    },
    {
        "name": "pre_llm_requires_hello",
        "handshake": False,
        "req": {"id": 3, "method": "pre_llm", "params": {}},
        "expect": {"error.code": "protocol"},
    },
    {
        "name": "pre_llm_deny_budget",
        "setup": {"method": "reset", "params": {"limit_tokens": 1}},
        "prereq": {
            # bill one usage first so spent >= limit
            "req": {
                "id": 3.5,
                "method": "post_llm",
                "params": {"usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            }
        },
        "req": {
            "id": 4,
            "method": "pre_llm",
            "params": {"node_label": "n1", "messages": [{"role": "user", "content": "hi"}]},
        },
        "expect": {"result.proceed": False, "result.reason": "str"},
    },
    {
        "name": "post_llm_accumulates",
        "setup": {"method": "reset", "params": {"limit_tokens": 0}},
        "req": {
            "id": 5,
            "method": "post_llm",
            "params": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        },
        "expect": {"result.ok": True},
    },
    {
        "name": "meter_shape",
        "req": {"id": 6, "method": "meter", "params": {}},
        "expect": {
            "result.calls": "int",
            "result.budget_spent_tokens": "int",
            "result.budget_exceeded": "bool",
        },
    },
    {
        "name": "resolve_unknown_id",
        "req": {"id": 7, "method": "resolve", "params": {"approval_id": "nope", "approve": True}},
        "expect": {"error.code": "state"},
    },
    {
        "name": "unknown_method",
        "req": {"id": 8, "method": "nope", "params": {}},
        "expect": {"error.code": "protocol"},
    },
]


def check_shape(value, spec) -> bool:
    if spec == "any":
        return True
    if spec == "str":
        return isinstance(value, str)
    if spec == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "bool":
        return isinstance(value, bool)
    if spec == "list":
        return isinstance(value, list)
    if spec == "obj":
        return isinstance(value, dict)
    return value == spec


def _get_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def run_vectors(harness=None, extra_vectors=None, harness_factory=None) -> list:
    """Returns list of failure strings; empty = all vectors pass. Each
    vector runs on its own harness (fresh factory) so vectors are
    order-independent; a prebuilt `harness` is shared instead."""
    failures = []
    for vec in list(SIDECAR_VECTORS) + list(extra_vectors or []):
        name = vec.get("name", "?")
        h = harness_factory() if harness_factory is not None else (harness or Harnessd())
        if vec.get("handshake", True):
            hs = h.handle_sync(
                {"id": 0, "method": "hello", "params": {"protocol_version": 1}}
            )
            if "error" in hs:
                failures.append("%s: handshake failed: %s" % (name, hs["error"]))
                continue
        setup = vec.get("setup")
        if setup:
            h.handle_sync({"id": 0, "method": setup["method"], "params": setup["params"]})
        prereq = vec.get("prereq")
        if prereq:
            h.handle_sync(prereq["req"])
        resp = h.handle_sync(vec["req"])
        for path, spec in vec["expect"].items():
            value, found = _get_path(resp, path)
            if not found:
                failures.append("%s: missing path %s in %s" % (name, path, resp))
            elif not check_shape(value, spec):
                failures.append("%s: %s=%r fails spec %r" % (name, path, value, spec))
    return failures


def run_self_check() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    failures = run_vectors(harness_factory=Harnessd)
    if failures:
        for f in failures:
            logger.error("conformance FAIL: %s", f)
        return 1
    print("sidecar conformance: %s vectors PASS" % len(SIDECAR_VECTORS))
    return 0
