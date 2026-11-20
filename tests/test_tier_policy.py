"""Issue #142: deterministic per-phase tier policy hook."""

from fusion_harness import LifecycleBus, TIER_POLICY, SpawnBrief, recommend_tier
from fusion_harness.subagent import SubagentResult, SubagentRunner
from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import TeamRuntime, TeamTask


def _spy_bus():
    bus = LifecycleBus()
    events = []
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        events.append((event, payload))
        return orig(source, event, payload, session_id)

    bus.emit = spy
    return bus, events


class _NoopRunner(SubagentRunner):
    async def run(
        self,
        spec,
        agent_fn,
        parent_tools=None,
        parent_mode="default",
        event_sink=None,
        steer_queue=None,
    ):
        await agent_fn(spec, None)
        return SubagentResult(subagent_id="x", name=spec.name, status="done", summary="ok")


def _definition(**kw):
    return TeamDefinition(
        name="scan-team",
        namespace_key="team:scan-2026-11",
        members=[TeamMember(name="m1", role="worker", **kw.pop("member_kw", {}))],
    )


def _runtime(member_kw=None, bus=None, tmp_path=None):
    return TeamRuntime(
        _definition(member_kw=member_kw or {}),
        runner=_NoopRunner(),
        bus=bus,
        state_dir=str(tmp_path / "sd") if tmp_path else None,
    )


def _brief(**kw):
    params = dict(
        objective="scan module X",
        output_format="json findings list",
        tool_guidance="use ripgrep",
        boundaries="read-only",
        token_budget=5000,
        concurrency=1,
        depth=1,
    )
    params.update(kw)
    return SpawnBrief(**params)


async def _run_member(rt, bus_events, brief=None):
    rt.activate()
    task_id = rt.add_task(TeamTask(title="scan"))

    async def agent_fn(spec, session):
        return None

    await rt.run_member(rt.definition.member("m1"), task_id, brief or _brief(), agent_fn)
    return task_id, [e for e in bus_events if e[0] == "tier_recommendation"]


class TestTierPolicy:
    def test_table_contents(self):
        assert TIER_POLICY["tool_call"] == "cheap"
        assert TIER_POLICY["planning"] == "cheap"
        assert TIER_POLICY["member_execution"] == "cheap"
        assert TIER_POLICY["synthesis"] == "strong"
        assert TIER_POLICY["decision"] == "strong"
        assert TIER_POLICY["verification"] == "strong"

    def test_unknown_kind_fails_open_empty(self):
        assert recommend_tier("galaxy_brain") == ""
        assert recommend_tier("") == ""

    async def test_policy_default_wired_when_member_unspecified(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(bus=bus, tmp_path=tmp_path)
        captured = {}

        async def agent_fn(spec, session):
            captured.update(spec.metadata)
            return None

        rt.activate()
        task_id = rt.add_task(TeamTask(title="scan"))
        await rt.run_member(rt.definition.member("m1"), task_id, _brief(), agent_fn)
        assert captured["model_tier"] == "cheap"
        recs = [e for e in events if e[0] == "tier_recommendation"]
        assert len(recs) == 1
        payload = recs[0][1]
        assert payload["tier"] == "cheap"
        assert payload["source"] == "policy"
        assert payload["member"] == "m1"
        assert "policy" in payload["reason"]

    async def test_explicit_member_tier_overrides_policy(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(member_kw={"model_tier": "strong"}, bus=bus, tmp_path=tmp_path)
        captured = {}

        async def agent_fn(spec, session):
            captured.update(spec.metadata)
            return None

        rt.activate()
        task_id = rt.add_task(TeamTask(title="scan"))
        await rt.run_member(rt.definition.member("m1"), task_id, _brief(), agent_fn)
        assert captured["model_tier"] == "strong"
        recs = [e for e in events if e[0] == "tier_recommendation"]
        assert recs[0][1]["source"] == "explicit"

    async def test_brief_hint_beats_policy_loses_to_member(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(bus=bus, tmp_path=tmp_path)
        captured = {}

        async def agent_fn(spec, session):
            captured.update(spec.metadata)
            return None

        rt.activate()
        task_id = rt.add_task(TeamTask(title="scan"))
        await rt.run_member(
            rt.definition.member("m1"), task_id, _brief(model_tier="strong"), agent_fn
        )
        assert captured["model_tier"] == "strong"
        recs = [e for e in events if e[0] == "tier_recommendation"]
        assert recs[0][1]["source"] == "brief"

    async def test_zero_model_calls_preserved(self, tmp_path):
        bus, events = _spy_bus()
        rt = _runtime(bus=bus, tmp_path=tmp_path)
        _, recs = await _run_member(rt, events)
        assert len(recs) == 1  # recommendation is deterministic, no model in kernel
