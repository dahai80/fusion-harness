"""Tests for structured permission requests (#159)."""

import pytest

from fusion_harness import (
    ACTION_INTENTS,
    REQUEST_KINDS,
    PermissionRequest,
    PermissionRequestError,
    PermissionResponse,
    RequestAction,
    resolve_request,
)


def _plan_request():
    return PermissionRequest(
        session_id="s1",
        kind="plan",
        prompt="refactor auth module",
        actions=[
            RequestAction(behavior="approve", intent="implement"),
            RequestAction(behavior="reject", intent="dismiss"),
        ],
        suggestions=["split into two PRs"],
    )


def test_kind_and_intent_constants():
    assert REQUEST_KINDS == ("tool", "plan", "question", "mode", "other")
    assert ACTION_INTENTS == ("implement", "implement_resume", "dismiss")


def test_request_validates_kind():
    with pytest.raises(PermissionRequestError):
        PermissionRequest(session_id="s1", kind="banana", actions=[RequestAction("allow")])


def test_request_requires_actions():
    with pytest.raises(PermissionRequestError):
        PermissionRequest(session_id="s1", kind="tool", actions=[])


def test_action_validates_intent():
    with pytest.raises(PermissionRequestError):
        RequestAction(behavior="allow", intent="explode")


def test_request_assigns_id():
    req = _plan_request()
    assert req.id.startswith("preq_")


def test_resolve_picks_action_and_intent():
    req = _plan_request()
    resolved = resolve_request(req, PermissionResponse(request_id=req.id, behavior="approve"))
    assert resolved.behavior == "approve"
    assert resolved.intent == "implement"
    assert resolved.degenerate is False


def test_resolve_unknown_behavior_rejected():
    req = _plan_request()
    with pytest.raises(PermissionRequestError):
        resolve_request(req, PermissionResponse(request_id=req.id, behavior="allow"))


def test_resolve_wrong_request_id_rejected():
    req = _plan_request()
    with pytest.raises(PermissionRequestError):
        resolve_request(req, PermissionResponse(request_id="preq_other", behavior="approve"))


def test_follow_up_prompt_honored_for_implement():
    req = _plan_request()
    resolved = resolve_request(
        req,
        PermissionResponse(
            request_id=req.id, behavior="approve", follow_up_prompt="implement the plan"
        ),
    )
    assert resolved.follow_up_prompt == "implement the plan"


def test_follow_up_prompt_dropped_for_dismiss():
    req = _plan_request()
    resolved = resolve_request(
        req,
        PermissionResponse(
            request_id=req.id, behavior="reject", follow_up_prompt="implement the plan"
        ),
    )
    assert resolved.follow_up_prompt == ""


def test_degenerate_allow_deny_case():
    req = PermissionRequest(
        session_id="s1", kind="tool", prompt="run rm -rf", actions=[RequestAction("deny")]
    )
    resolved = resolve_request(req, PermissionResponse(request_id=req.id, behavior="deny"))
    assert resolved.degenerate is True
    assert resolved.behavior == "deny"
    assert resolved.intent == "dismiss"


def test_degenerate_false_when_multiple_actions():
    req = PermissionRequest(
        session_id="s1",
        kind="tool",
        actions=[RequestAction("allow", intent="implement"), RequestAction("deny")],
    )
    resolved = resolve_request(req, PermissionResponse(request_id=req.id, behavior="allow"))
    assert resolved.degenerate is False
    assert resolved.intent == "implement"
