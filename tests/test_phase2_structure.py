"""Phase 2 structure tests: checkpoints (P2-2), subagent runtime (P2-1),
skills + permission derivation (P2-3), eval baseline gate (P2-4)."""

import pytest

from fusion_harness.checkpoint import CheckpointState, CheckpointStore
from fusion_harness.funnels import LifecycleBus
from fusion_harness.skills import SkillError, SkillRegistry, derive_permissions, parse_skill
from fusion_harness.subagent import SubagentRunner, SubagentSpec


class TestCheckpoints:
    def test_save_latest_load_roundtrip(self):
        store = CheckpointStore(":memory:")
        state = CheckpointState(
            messages=[{"role": "user", "content": "hi"}],
            budget_spent_tokens=120,
            permission_mode="default",
            outbox_offset=7,
        )
        seq = store.save("s1", state)
        assert seq == 1
        loaded = store.latest("s1")
        assert loaded.budget_spent_tokens == 120
        assert loaded.outbox_offset == 7
        assert loaded.messages[0]["content"] == "hi"
        assert store.load("s1", seq).permission_mode == "default"
        assert store.load("s1", 99) is None
        assert store.latest("missing") is None

    def test_seq_increments_and_list(self):
        store = CheckpointStore(":memory:")
        store.save("s1", CheckpointState())
        store.save("s1", CheckpointState(budget_spent_tokens=5))
        store.save("s1", CheckpointState(budget_spent_tokens=9))
        assert [c["seq"] for c in store.list_checkpoints("s1")] == [1, 2, 3]
        assert store.latest("s1").budget_spent_tokens == 9

    def test_prune_keeps_tail(self):
        store = CheckpointStore(":memory:")
        for i in range(15):
            store.save("s1", CheckpointState(budget_spent_tokens=i))
        removed = store.prune("s1", keep_last=10)
        assert removed == 5
        assert len(store.list_checkpoints("s1")) == 10
        assert store.latest("s1").budget_spent_tokens == 14

    def test_resume_preserves_state_not_effects(self, tmp_path):
        """Checkpoint -> crash -> reopen: state loads from disk; the outbox
        receipts (separate store) own effect idempotency — documented seam."""
        path = str(tmp_path / "cp.db")
        store = CheckpointStore(path)
        store.save(
            "s1", CheckpointState(messages=[{"role": "user", "content": "task"}], outbox_offset=3)
        )
        store.close()
        reopened = CheckpointStore(path)
        state = reopened.latest("s1")
        assert state.outbox_offset == 3


class TestSubagentRuntime:
    @pytest.mark.asyncio
    async def test_summary_only_return_and_events(self):
        bus = LifecycleBus()
        runner = SubagentRunner(bus=bus)
        events = []
        bus.subscribe(lambda ev: events.append(ev.event))

        async def agent(spec, session):
            session.record_turn(tokens=10)
            return "found 3 bugs"

        result = await runner.run(SubagentSpec(name="scan", prompt="scan repo"), agent)
        assert result.status == "completed"
        assert result.summary == "found 3 bugs"
        assert "subagent_start" in events and "subagent_end" in events

    @pytest.mark.asyncio
    async def test_tool_allowlist_narrows_parent_surface(self):
        runner = SubagentRunner()

        async def agent(spec, session):
            assert session.check_tool("read_file") is True
            assert session.check_tool("bash") is False
            return "ok"

        result = await runner.run(
            SubagentSpec(name="reader", prompt="read only", tool_allowlist=["read_file"]),
            agent,
            parent_tools=["read_file", "bash", "write_file"],
        )
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_permission_mode_never_widens(self):
        runner = SubagentRunner()
        assert (
            runner.narrow_mode("plan", SubagentSpec(name="x", prompt="p", permission_mode="bypass"))
            == "plan"
        )
        assert (
            runner.narrow_mode("bypass", SubagentSpec(name="x", prompt="p", permission_mode="plan"))
            == "plan"
        )
        assert (
            runner.narrow_mode(
                "default", SubagentSpec(name="x", prompt="p", permission_mode="plan")
            )
            == "plan"
        )
        assert (
            runner.narrow_mode(
                "default", SubagentSpec(name="x", prompt="p", permission_mode="approved")
            )
            == "default"
        )

    @pytest.mark.asyncio
    async def test_budget_cap_flags_capped(self):
        runner = SubagentRunner()

        async def agent(spec, session):
            for _ in range(5):
                session.record_turn(tokens=400)
            return "partial"

        result = await runner.run(SubagentSpec(name="hog", prompt="p", budget_tokens=1000), agent)
        assert result.status == "capped"
        assert "budget_exceeded" in (result.error or "") or result.tokens_spent == 2000

    @pytest.mark.asyncio
    async def test_agent_failure_is_visible(self):
        runner = SubagentRunner()

        async def agent(spec, session):
            raise RuntimeError("boom")

        result = await runner.run(SubagentSpec(name="crash", prompt="p"), agent)
        assert result.status == "failed"
        assert "boom" in result.error

    def test_spec_validation(self):
        with pytest.raises(ValueError):
            SubagentSpec(name="", prompt="p")
        with pytest.raises(ValueError):
            SubagentSpec(name="x", prompt="p", permission_mode="chaos")


class TestSkills:
    def test_parse_and_derive(self):
        text = """---
name: deploy-docs
description: Build and deploy the docs site
permissions: allow_tools: build_docs, deploy_docs; deny_tools: bash; workspace: docs/
---

Deploy steps go here."""
        skill = parse_skill(text, source="skills/deploy-docs.md")
        assert skill.name == "deploy-docs"
        rules = derive_permissions(skill)
        allow = [r for r in rules if r["action"] == "allow"]
        deny = [r for r in rules if r["action"] == "deny"]
        assert {r["tool"] for r in allow} == {"^build_docs$", "^deploy_docs$"}
        assert all(r["layer"] == "node" for r in rules)
        assert len(deny) == 1 and deny[0]["tool"] == "^bash$"
        assert allow[0]["path_scope"] == ["docs/"]

    def test_deny_wins_conflict(self):
        skill = parse_skill(
            "---\nname: x\ndescription: d\npermissions: allow_tools: bash, read_file; deny_tools: bash\n---\nbody"
        )
        rules = derive_permissions(skill)
        allows = [r for r in rules if r["action"] == "allow"]
        denies = [r for r in rules if r["action"] == "deny"]
        assert [r["tool"] for r in allows] == ["^read_file$"]
        assert len(denies) == 1

    def test_glob_allow(self):
        skill = parse_skill(
            "---\nname: g\ndescription: d\npermissions: allow_tools: mcp__docs__*\n---\nb"
        )
        rules = derive_permissions(skill)
        assert rules[0]["tool"] == "^mcp__docs__.*$"

    def test_missing_name_rejected(self):
        with pytest.raises(SkillError):
            parse_skill("no frontmatter here")

    def test_unknown_permission_key_rejected(self):
        with pytest.raises(SkillError):
            parse_skill("---\nname: x\ndescription: d\npermissions: root_all: yes\n---\nb")

    def test_registry_dup_and_lookup(self):
        reg = SkillRegistry()
        skill = parse_skill("---\nname: a\ndescription: d\n---\nbody")
        reg.register(skill)
        with pytest.raises(SkillError):
            reg.register(parse_skill("---\nname: a\ndescription: d\n---\nbody2"))
        assert reg.names() == ["a"]
        assert reg.derived_rules("a") == []
        with pytest.raises(KeyError):
            reg.derived_rules("nope")
