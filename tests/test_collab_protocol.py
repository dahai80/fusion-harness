"""Collaboration discipline tests (issues #26 #27 #28 #29):
spawn brief contract + effort tiers, artifact-summary dual channel,
playbook append protocol, KV-cache discipline."""

import os

import pytest

from fusion_harness.artifact_channel import ArtifactStore, SummaryInjector, WorkerDelivery
from fusion_harness.cache_discipline import (
    ToolAvailabilityState,
    assert_stable_prefix,
    check_append_only,
    mask_tools,
)
from fusion_harness.playbook import Playbook, PlaybookOp
from fusion_harness.spawn_protocol import (
    EffortTierConfig,
    SpawnBrief,
    SpawnBriefError,
    brief_from_event,
    validate_brief,
)


class TestSpawnBrief:
    def test_complete_brief_valid(self):
        brief = SpawnBrief(
            objective="scan repo X",
            output_format="json list of findings",
            tool_guidance="use ripgrep only",
            boundaries="do not modify files",
            token_budget=5000,
            concurrency=2,
            depth=1,
        )
        assert validate_brief(brief) == []

    def test_incomplete_brief_raises_typed(self):
        with pytest.raises(SpawnBriefError) as exc:
            SpawnBrief(
                objective="do stuff",
                output_format="",
                tool_guidance="t",
                boundaries="b",
                token_budget=0,
                concurrency=1,
                depth=1,
            )
        assert "output_format" in str(exc.value)
        assert "token_budget" in str(exc.value)

    def test_brief_from_event_missing_fields_fails(self):
        with pytest.raises(SpawnBriefError):
            brief_from_event({"objective": "x"})

    def test_depth_guard(self):
        with pytest.raises(SpawnBriefError):
            SpawnBrief(
                objective="o",
                output_format="f",
                tool_guidance="g",
                boundaries="b",
                token_budget=100,
                concurrency=1,
                depth=4,
            )


class TestEffortTiers:
    def test_single_agent_low_calls(self):
        tier = EffortTierConfig().resolve(3)
        assert tier.name == "single" and tier.workers == 1

    def test_small_team_mid_calls(self):
        tier = EffortTierConfig().resolve(7)
        assert tier.name == "small_team"
        assert 2 <= tier.workers <= 4
        assert tier.split_responsibilities is True

    def test_large_split_high_calls(self):
        tier = EffortTierConfig().resolve(160)
        assert tier.name == "large_split"
        assert tier.workers >= 10

    def test_deterministic_no_model(self):
        cfg = EffortTierConfig()
        assert cfg.resolve(50) == cfg.resolve(50)


class TestArtifactChannel:
    def test_artifact_written_brain_gets_summary_and_ref(self, tmp_path):
        store = ArtifactStore(str(tmp_path))
        injector = SummaryInjector()
        ref = store.write_artifact("run-1", "scanner", "decision: use ripgrep\n" + "filler\n" * 200)
        assert os.path.exists(store.artifact_path(ref))
        assert "decision: use ripgrep" in open(store.artifact_path(ref)).read()
        summary = injector.summarize_delivery("scanner", "decision: use ripgrep\n" + "x\n" * 300)
        deliveries = [WorkerDelivery(worker_name="scanner", artifact_ref=ref, summary=summary)]
        msgs = [{"role": "system", "content": "rules"}] + [
            {"role": "user", "content": "old %d" % i} for i in range(20)
        ]
        out = injector.inject_front(msgs, deliveries)
        assert "Worker summaries" in str(out[1]["content"])
        assert ref in str(out[1]["content"])
        assert out[1]["content"].index("Worker summaries") >= 0
        assert out[2:] == msgs[1:]

    def test_inject_idempotent(self, tmp_path):
        injector = SummaryInjector()
        d = [WorkerDelivery(worker_name="w", artifact_ref="r", summary="s")]
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
        once = injector.inject_front(msgs, d)
        twice = injector.inject_front(once, d)
        assert len(twice) == len(once)


class TestPlaybook:
    def test_roundtrip_preserves_detail_where_rewrite_loses_it(self):
        book = Playbook("deploy")
        detail = "domain rule: alpha-beta cache warms only on tuesday deploys"
        book.apply(PlaybookOp(op="add", section="rules", content=detail, episodes=["ep1"]))
        book.apply(
            PlaybookOp(op="add", section="rules", content="short rule two", episodes=["ep2"])
        )
        book.apply(
            PlaybookOp(
                op="update",
                section="rules",
                content="short rule two (revised)",
                episodes=["ep3"],
                target_id="deploy-e2",
            )
        )
        rendered = book.render()
        assert detail in rendered
        assert "revised" in rendered
        naive = " | ".join(["latest summary only"]) * 1
        assert detail not in naive

    def test_provenance_traceable(self):
        book = Playbook("p")
        book.apply(PlaybookOp(op="add", section="s", content="c1", episodes=["ep1"]))
        book.apply(
            PlaybookOp(op="update", section="s", content="c2", episodes=["ep2"], target_id="p-e1")
        )
        assert book.trace("p-e1") == ["ep1", "ep2"]

    def test_delete_tombstone_and_validation(self):
        book = Playbook("p")
        book.apply(PlaybookOp(op="add", section="s", content="c", episodes=["e1"]))
        book.apply(
            PlaybookOp(op="delete", section="s", content="", episodes=["e2"], target_id="p-e1")
        )
        assert "c" not in book.render()
        with pytest.raises(ValueError):
            PlaybookOp(op="add", section="s", content="x", episodes=[])
        with pytest.raises(ValueError):
            PlaybookOp(op="update", section="s", content="x", episodes=["e"], target_id="")


class TestCacheDiscipline:
    def test_stable_prefix_ok_and_violation(self):
        base = "system rules | tools: a,b,c |"
        stable = [base, base + " turn1", base + " turn1 turn2"]
        assert assert_stable_prefix(stable) == []
        volatile = [base + " ts=1", base + " ts=999"]
        unstable = assert_stable_prefix(volatile)
        assert unstable == [1]

    def test_append_only_enforced(self):
        before = [{"role": "user", "content": "a"}]
        good = before + [{"role": "assistant", "content": "b"}]
        assert check_append_only(before, good) is True
        shrunk = []
        assert check_append_only(before, shrunk) is False
        assert check_append_only(before, shrunk, compaction=True) is True
        mutated = [{"role": "user", "content": "CHANGED"}] + good[1:]
        assert check_append_only(before, mutated) is False

    def test_mask_never_removes(self):
        tools = [{"name": n, "description": ""} for n in ("read", "write", "bash")]
        masked = mask_tools(tools, ["read"])
        assert len(masked) == 3
        flags = {t["name"]: t["x-fusion-masked"] for t in masked}
        assert flags == {"read": False, "write": True, "bash": True}

    def test_state_transition_masking_no_cache_invalidation(self):
        tools = [{"name": n} for n in ("a", "b", "c")]
        state = ToolAvailabilityState(tools, ["a", "b"])
        assert state.masked()[2]["x-fusion-masked"] is True
        state.transition(["a", "c"])
        masked = state.masked()
        assert len(masked) == len(tools)
        assert [t["name"] for t in masked] == ["a", "b", "c"]
        assert masked[1]["x-fusion-masked"] is True and masked[2]["x-fusion-masked"] is False
