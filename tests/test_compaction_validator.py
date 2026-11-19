"""Issue #131: post-compaction state validation hook + lifecycle event."""

from fusion_harness import CompactionConfig, Compactor, LifecycleBus


def _bus():
    bus = LifecycleBus()
    events = []

    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        events.append((event, payload))
        return orig(source, event, payload, session_id)

    bus.emit = spy
    return bus, events


def _msgs(n=20):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "msg %d" % i} for i in range(n)]


class TestCompactionValidator:
    def test_default_none_zero_change(self):
        bus, events = _bus()
        compactor = Compactor(CompactionConfig(), lifecycle_bus=bus)
        out = compactor.compact(_msgs(), "warning")
        assert out
        assert not any(e[0] == "compaction_validated" for e in events)

    def test_validator_pass_emits_ok_event(self):
        bus, events = _bus()
        calls = []

        def validator(state, original_tokens):
            calls.append((len(state), original_tokens))
            return True

        compactor = Compactor(CompactionConfig(validator=validator), lifecycle_bus=bus)
        compactor.compact(_msgs(), "warning")
        assert calls
        event = next(e for e in events if e[0] == "compaction_validated")
        payload = event[1]
        assert payload["ok"] is True
        assert payload["verdict"] == ""
        assert payload["after_tokens"] <= payload["before_tokens"]

    def test_falsy_verdict_warns_not_gates(self):
        bus, events = _bus()

        def validator(state, original_tokens):
            return False, "task goal missing"

        compactor = Compactor(CompactionConfig(validator=validator), lifecycle_bus=bus)
        before = _msgs()
        out = compactor.compact(before, "warning")
        assert out  # compaction result NOT reverted or gated
        event = next(e for e in events if e[0] == "compaction_validated")
        assert event[1]["ok"] is False
        assert event[1]["verdict"] == "task goal missing"

    def test_validator_exception_fail_safe(self):
        bus, events = _bus()

        def validator(state, original_tokens):
            raise RuntimeError("boom")

        compactor = Compactor(CompactionConfig(validator=validator), lifecycle_bus=bus)
        out = compactor.compact(_msgs(), "warning")
        assert out
        event = next(e for e in events if e[0] == "compaction_validated")
        assert event[1]["ok"] is False
        assert "boom" in event[1]["verdict"]

    def test_no_bus_no_crash(self):
        def validator(state, original_tokens):
            return False, "x"

        compactor = Compactor(CompactionConfig(validator=validator))
        assert compactor.compact(_msgs(), "warning")
