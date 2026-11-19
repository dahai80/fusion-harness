import pytest

from fusion_harness.team_topology import (
    TOPOLOGY_BANK,
    TopologyError,
    TopologySelector,
    TopologyTelemetry,
)

ROLES = {"lead": "lead", "coder": "coder", "reviewer": "reviewer"}
COMPLEMENTS = {"lead": {"coder", "reviewer"}}


def test_bank_is_data_driven():
    assert set(TOPOLOGY_BANK) == {"chain", "star", "mesh", "role_filtered", "lead_only"}
    for spec in TOPOLOGY_BANK.values():
        edges = spec.edges(ROLES)
        assert isinstance(edges, list)


def test_bank_edge_shapes():
    assert TOPOLOGY_BANK["chain"].edges(ROLES) == [
        ("coder", "lead"),
        ("lead", "reviewer"),
    ]
    assert TOPOLOGY_BANK["star"].edges(ROLES) == [
        ("lead", "coder"),
        ("lead", "reviewer"),
    ]
    assert len(TOPOLOGY_BANK["mesh"].edges(ROLES)) == 6
    assert TOPOLOGY_BANK["lead_only"].edges(ROLES) == []


def test_role_filtered_only_same_role_pairs():
    roles = {"a": "coder", "b": "coder", "c": "reviewer"}
    edges = TOPOLOGY_BANK["role_filtered"].edges(roles)
    assert ("a", "b") in edges and ("b", "a") in edges
    assert all(roles[s] == roles[d] for s, d in edges)


def test_role_fit_prefers_complementary_graph():
    selector = TopologySelector(role_complements=COMPLEMENTS, unseen_message_tokens=10)
    name, _ = selector.select(ROLES, remaining_budget=10_000)
    assert name == "star"


def test_budget_filter_drops_mesh_keeps_star():
    selector = TopologySelector(unseen_message_tokens=100)
    eligible = selector.eligible(ROLES, remaining_budget=1000)
    assert "mesh" not in eligible
    assert "star" in eligible


def test_star_chosen_when_budget_tight():
    selector = TopologySelector(role_complements=COMPLEMENTS, unseen_message_tokens=100)
    name, edges = selector.select(ROLES, remaining_budget=800)
    assert name == "star"
    assert edges == [("lead", "coder"), ("lead", "reviewer")]


def test_no_topology_fits_raises():
    from fusion_harness.team_topology import TopologySpec

    bank = {"mesh": TOPOLOGY_BANK["mesh"], "chain": TOPOLOGY_BANK["chain"]}
    for name, spec in bank.items():
        bank[name] = TopologySpec(spec.name, spec.edges_fn, spec.max_fanout)
    selector = TopologySelector(bank=bank, unseen_message_tokens=100)
    with pytest.raises(TopologyError):
        selector.select(ROLES, remaining_budget=1)


def test_hysteresis_prefers_previous_topology():
    selector = TopologySelector(role_complements=COMPLEMENTS, unseen_message_tokens=10)
    first, _ = selector.select(ROLES, remaining_budget=10_000)
    selector.observe("lead", 5)
    second, _ = selector.select(ROLES, remaining_budget=10_000)
    assert first == second


def test_hysteresis_yields_to_hard_budget():
    complements = {"coder": {"reviewer"}, "reviewer": {"coder"}}
    selector = TopologySelector(
        role_complements=complements, unseen_message_tokens=100, cost_weight=0.0001
    )
    first, _ = selector.select(ROLES, remaining_budget=10_000)
    assert first == "mesh"
    name, _ = selector.select(ROLES, remaining_budget=800)
    assert name != "mesh"


def test_role_fit_signal_tips_score():
    selector = TopologySelector(role_complements=COMPLEMENTS, unseen_message_tokens=10)
    selector._last_size = {n: 10 for n in ROLES}
    fit_edges = [("lead", "coder"), ("lead", "reviewer")]
    nofit_edges = [("lead", "x"), ("x", "y")]
    assert selector._role_fit(fit_edges, ROLES) == 1.0
    assert selector._role_fit(nofit_edges, {"lead": "lead", "x": "other", "y": "other"}) == 0.0
    assert selector._role_fit([], ROLES) == 0.0


def test_no_complement_map_means_full_fit():
    selector = TopologySelector()
    assert selector._role_fit([("a", "b")], {"a": "x", "b": "y"}) == 1.0


def test_observe_feeds_cost_estimate():
    selector = TopologySelector(unseen_message_tokens=64)
    base = selector._est_token_cost([("a", "b")])
    selector.observe("a", 500)
    grown = selector._est_token_cost([("a", "b")])
    assert grown == base - 64 + 500


def test_observe_rejects_negative():
    selector = TopologySelector()
    with pytest.raises(TopologyError):
        selector.observe("a", -1)


def test_telemetry_records_steps_and_skips():
    telemetry = TopologyTelemetry()
    telemetry.record_step(
        1,
        "star",
        edge_sends={("lead", "coder"): 120},
        skipped_edges=[("lead", "reviewer")],
    )
    entry = telemetry.summary()[0]
    assert entry["topology"] == "star"
    assert entry["edge_sends"] == {"lead->coder": 120}
    assert entry["skipped_edges"] == ["lead->reviewer"]


def test_selector_record_step_uses_internal_step_counter():
    selector = TopologySelector(unseen_message_tokens=10)
    selector.select(ROLES, remaining_budget=10_000)
    selector.record_step("mesh", edge_sends={("a", "b"): 5})
    entry = selector.telemetry.summary()[0]
    assert entry["step"] == 1 and entry["topology"] == "mesh"


def test_custom_bank_entry_needs_no_engine_change():
    from fusion_harness.team_topology import TopologySpec

    def pair_up(roles):
        names = sorted(roles)
        return list(zip(names[::2], names[1::2]))

    bank = {"pair": TopologySpec("pair", pair_up, max_fanout=1)}
    selector = TopologySelector(bank=bank, unseen_message_tokens=10)
    name, edges = selector.select(ROLES, remaining_budget=10_000)
    assert name == "pair"
    assert ("coder", "lead") in edges


def test_empty_roles_rejected():
    selector = TopologySelector()
    with pytest.raises(TopologyError):
        selector.select({}, remaining_budget=1000)
    with pytest.raises(TopologyError):
        selector.eligible({}, remaining_budget=1000)


def test_negative_budget_rejected():
    selector = TopologySelector()
    with pytest.raises(TopologyError):
        selector.eligible(ROLES, remaining_budget=-5)


def test_max_fanout_filters_candidate():
    from fusion_harness.team_topology import TopologySpec

    roles = dict(ROLES, extra="worker")
    bank = {"wide": TopologySpec("wide", TOPOLOGY_BANK["mesh"].edges_fn, max_fanout=2)}
    selector = TopologySelector(bank=bank, unseen_message_tokens=10)
    assert selector.eligible(roles, remaining_budget=10_000) == []
    assert selector.eligible(ROLES, remaining_budget=10_000) == ["wide"]
