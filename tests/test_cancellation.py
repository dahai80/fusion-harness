"""Tests for cancellation idempotency contract (#156)."""

import pytest

from fusion_harness import (
    CancelToken,
    CancellationRegistry,
    LifecycleBus,
    TurnStateError,
)


def _spy_bus():
    bus = LifecycleBus()
    seen = []
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        seen.append((event, payload))
        return orig(source, event, payload, session_id=session_id)

    bus.emit = spy
    return bus, seen


def test_cancel_idempotent_double_call():
    reg = CancellationRegistry()
    token = reg.begin_turn("s1", "t1")
    assert reg.cancel(token) is True
    assert reg.cancel(token) is True
    assert reg.current_turn("s1") == "t1"


def test_cancel_emits_event_once():
    bus, seen = _spy_bus()
    reg = CancellationRegistry(bus=bus)
    token = reg.begin_turn("s1", "t1")
    reg.cancel(token)
    reg.cancel(token)
    cancelled = [p for e, p in seen if e == "turn_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["turn_id"] == "t1"


def test_stale_token_is_noop_never_settles_new_turn():
    reg = CancellationRegistry()
    old = reg.begin_turn("s1", "t1")
    reg.begin_turn("s1", "t2")
    assert reg.cancel(old) is False
    assert reg.current_turn("s1") == "t2"
    assert reg.is_ambiguous("s1") is False


def test_stale_token_does_not_emit_event():
    bus, seen = _spy_bus()
    reg = CancellationRegistry(bus=bus)
    old = reg.begin_turn("s1", "t1")
    reg.begin_turn("s1", "t2")
    reg.cancel(old)
    assert [e for e, _ in seen if e == "turn_cancelled"] == []


def test_ambiguous_rejects_new_work():
    reg = CancellationRegistry()
    reg.begin_turn("s1", "t1")
    assert reg.mark_ambiguous("s1", "t1") is True
    with pytest.raises(TurnStateError):
        reg.begin_turn("s1", "t2")


def test_mark_ambiguous_ignores_non_current_turn():
    reg = CancellationRegistry()
    reg.begin_turn("s1", "t1")
    assert reg.mark_ambiguous("s1", "t-other") is False
    assert reg.is_ambiguous("s1") is False


def test_rebind_clears_ambiguity_and_issues_token():
    bus, seen = _spy_bus()
    reg = CancellationRegistry(bus=bus)
    reg.begin_turn("s1", "t1")
    reg.mark_ambiguous("s1", "t1")
    token = reg.rebind("s1", "t2")
    assert isinstance(token, CancelToken)
    assert reg.is_ambiguous("s1") is False
    assert reg.current_turn("s1") == "t2"
    assert reg.cancel(token) is True
    rebinding = [p for e, p in seen if e == "turn_rebind"]
    assert len(rebinding) == 1


def test_new_turn_after_clean_cancel_allowed():
    reg = CancellationRegistry()
    token = reg.begin_turn("s1", "t1")
    reg.cancel(token)
    token2 = reg.begin_turn("s1", "t2")
    assert reg.cancel(token2) is True


def test_token_carries_turn_identity():
    reg = CancellationRegistry()
    token = reg.begin_turn("s1", "t1")
    assert token.session_id == "s1"
    assert token.turn_id == "t1"
    assert token.token_id


def test_repeated_cancel_across_tokens_same_turn():
    reg = CancellationRegistry()
    t1 = reg.begin_turn("s1", "turn-a")
    t2 = CancelToken(session_id="s1", turn_id="turn-a", token_id=t1.token_id)
    assert reg.cancel(t1) is True
    assert reg.cancel(t2) is True
