"""TeamRuntime topology-scheduling wiring tests (issue #101 phase 2).

Default (env unset, no constructor arg) is OFF — broadcast behavior is
byte-identical to the pre-wiring runtime. Constructor arg wins over the
env flag. The selector proposes the send set; the wired budget disposes;
a selector fault falls back to a loud full broadcast.
"""

from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import TOPOLOGY_ENV_FLAG, TeamRuntime
from fusion_harness.team_topology import TopologyError, TopologySelector, TopologyTelemetry


def temp_dir(tmp_path=None):
    import tempfile

    return tempfile.mkdtemp(prefix="fh-wiring-")


def _member(name, **kw):
    params = dict(role="worker")
    params.update(kw)
    return TeamMember(name=name, **params)


def _star_selector(**kw):
    # lead->worker complements make star the unique fit-1.0 topology over
    # the default bank (chain scores 0.5, mesh/role_filtered lower).
    params = dict(role_complements={"lead": {"worker"}})
    params.update(kw)
    return TopologySelector(**params)


def _definition(**kw):
    params = dict(
        name="topo-team",
        namespace_key="team:topo-2026-11",
        members=[_member("lead", role="lead"), _member("worker1"), _member("worker2")],
    )
    params.update(kw)
    return TeamDefinition(**params)


class _Budget:
    def __init__(self, exceeded=False, spent=0):
        self._exceeded = exceeded
        self.spent_tokens = spent

    def is_exceeded(self, *args):
        return self._exceeded


class _BrokenSelector:
    bank = {"star": object()}

    def select(self, roles, remaining_budget):
        raise TopologyError("broken on purpose")

    def record_send(self, edge, tokens):
        pass

    def record_step(self, *args, **kwargs):
        pass


class TestTopologyWiring:
    def test_default_off_broadcast_unchanged(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        rt.activate()
        assert rt.enable_topology_scheduling is False
        assert rt.broadcast("lead", "standup") == 2
        assert len(rt.inbox("worker1")) == 1
        assert len(rt.inbox("worker2")) == 1

    def test_env_flag_arms(self, monkeypatch):
        monkeypatch.setenv(TOPOLOGY_ENV_FLAG, "1")
        rt = TeamRuntime(_definition(), state_dir=temp_dir())
        assert rt.enable_topology_scheduling is True
        assert isinstance(rt.topology_selector, TopologySelector)

    def test_constructor_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(TOPOLOGY_ENV_FLAG, "1")
        rt = TeamRuntime(_definition(), state_dir=temp_dir(), enable_topology_scheduling=False)
        assert rt.enable_topology_scheduling is False
        assert rt.broadcast("lead", "standup") == 2

    def test_star_filters_non_lead_broadcast(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        rt = TeamRuntime(
            _definition(),
            state_dir=temp_dir(),
            enable_topology_scheduling=True,
            topology_selector=_star_selector(),
        )
        rt.activate()
        # star edges go lead -> others; a worker broadcast proposes zero sends
        assert rt.broadcast("worker1", "gossip") == 0
        assert rt.inbox("worker2") == []
        assert rt.inbox("lead") == []
        assert len(rt.topology_selector.telemetry.steps) == 1

    def test_lead_broadcast_under_star_delivers_all(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        rt = TeamRuntime(
            _definition(),
            state_dir=temp_dir(),
            enable_topology_scheduling=True,
            topology_selector=_star_selector(),
        )
        rt.activate()
        assert rt.broadcast("lead", "standup") == 2
        assert len(rt.inbox("worker1")) == 1
        assert len(rt.inbox("worker2")) == 1
        step = rt.topology_selector.telemetry.steps[0]
        assert step["topology"] == "star"
        assert set(step["edge_sends"]) == {"lead->worker1", "lead->worker2"}

    def test_budget_disposes_denied_sends(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        rt = TeamRuntime(
            _definition(lead_budget_tokens=100000),
            state_dir=temp_dir(),
            enable_topology_scheduling=True,
            topology_selector=_star_selector(),
            budget_ref=_Budget(exceeded=True),
        )
        rt.activate()
        # selector proposes (star, lead -> workers) but the budget gate denies each send
        assert rt.broadcast("lead", "standup") == 0
        assert rt.inbox("worker1") == []
        assert rt.inbox("worker2") == []
        step = rt.topology_selector.telemetry.steps[0]
        assert set(step["skipped_edges"]) == {"lead->worker1", "lead->worker2"}

    def test_selector_fault_falls_back_to_broadcast(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        rt = TeamRuntime(
            _definition(),
            state_dir=temp_dir(),
            enable_topology_scheduling=True,
            topology_selector=_BrokenSelector(),
        )
        rt.activate()
        assert rt.broadcast("lead", "standup") == 2
        assert len(rt.inbox("worker1")) == 1
        assert len(rt.inbox("worker2")) == 1

    def test_custom_selector_and_telemetry_wired(self, monkeypatch):
        monkeypatch.delenv(TOPOLOGY_ENV_FLAG, raising=False)
        selector = TopologySelector(telemetry=TopologyTelemetry())
        rt = TeamRuntime(
            _definition(),
            state_dir=temp_dir(),
            enable_topology_scheduling=True,
            topology_selector=selector,
        )
        assert rt.topology_selector is selector
        rt.activate()
        rt.broadcast("lead", "standup")
        assert len(selector.telemetry.steps) == 1
