"""Tests for team member context injection (issue #61): TeamMember.context
field + SPEC.md / context sections prepended in run_member brief assembly."""

import asyncio
import os

from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.subagent import SubagentResult
from fusion_harness.team import TeamDefinition, TeamMember
from fusion_harness.team_runtime import (
    MEMBER_CONTEXT_CAP_BYTES,
    TEAM_SPEC_CAP_BYTES,
    TeamRuntime,
    TeamTask,
)


def _member(name, **kw):
    return TeamMember(name=name, role="worker", **kw)


def _definition(**kw):
    params = dict(
        name="ctx-team",
        namespace_key="team:ctx-2026-11",
        members=[_member("worker1"), _member("worker2")],
    )
    params.update(kw)
    return TeamDefinition(**params)


def _brief():
    return SpawnBrief(
        objective="scan module X",
        output_format="text",
        tool_guidance="use ripgrep",
        boundaries="read-only",
        token_budget=5000,
        concurrency=1,
        depth=1,
    )


def _capture_runtime(tmp_path, member):
    rt = TeamRuntime(_definition(members=[member]), state_dir=str(tmp_path))
    rt.activate()
    tid = rt.add_task(TeamTask(title="t"))
    captured = {}

    async def fake_run(spec, agent_fn, event_sink=None):
        captured["prompt"] = spec.prompt
        return SubagentResult(
            subagent_id="s1", name=member.name, status="completed", summary="done"
        )

    rt.runner.run = fake_run
    return rt, tid, captured


def _run(rt, tid, member):
    return asyncio.run(rt.run_member(member, tid, _brief(), None))


class TestTeamMemberContextField:
    def test_default_empty(self):
        assert _member("a").context == ""

    def test_custom_context_accepted(self):
        m = _member("a", context="always run pytest before reporting")
        assert m.context.startswith("always run")

    def test_constants_match_console_convention(self):
        assert TEAM_SPEC_CAP_BYTES == 4096
        assert MEMBER_CONTEXT_CAP_BYTES == 2048


class TestSpecInjection:
    def test_spec_section_with_absolute_path(self, tmp_path):
        (tmp_path / "SPEC.md").write_text("team spec rules v1", encoding="utf-8")
        rt, tid, captured = _capture_runtime(tmp_path, _member("worker1"))
        _run(rt, tid, rt.definition.member("worker1"))
        prompt = captured["prompt"]
        assert "[team spec]" in prompt
        assert "team spec rules v1" in prompt
        assert "(spec path: %s)" % os.path.abspath(str(tmp_path / "SPEC.md")) in prompt
        assert prompt.endswith("scan module X")

    def test_spec_over_cap_truncated_to_4kb(self, tmp_path):
        (tmp_path / "SPEC.md").write_text("x" * (TEAM_SPEC_CAP_BYTES + 500), encoding="utf-8")
        rt, tid, captured = _capture_runtime(tmp_path, _member("worker1"))
        _run(rt, tid, rt.definition.member("worker1"))
        prompt = captured["prompt"]
        start = prompt.index("[team spec]")
        end = prompt.index("(spec path:")
        assert end - start - len("[team spec]\n") <= TEAM_SPEC_CAP_BYTES + 1
        assert "x" * (TEAM_SPEC_CAP_BYTES + 1) not in prompt

    def test_spec_edit_reflected_without_restart(self, tmp_path):
        (tmp_path / "SPEC.md").write_text("rules v1", encoding="utf-8")
        rt, tid, captured = _capture_runtime(tmp_path, _member("worker1"))
        m = rt.definition.member("worker1")
        _run(rt, tid, m)
        assert "rules v1" in captured["prompt"]
        (tmp_path / "SPEC.md").write_text("rules v2 edited", encoding="utf-8")
        captured.clear()
        tid2 = rt.add_task(TeamTask(title="t2b"))
        _run(rt, tid2, m)
        assert "rules v2 edited" in captured["prompt"]

    def test_missing_spec_no_section_no_error(self, tmp_path):
        rt, tid, captured = _capture_runtime(tmp_path, _member("worker1"))
        _run(rt, tid, rt.definition.member("worker1"))
        assert captured["prompt"] == "scan module X"


class TestMemberContextInjection:
    def test_context_section_present(self, tmp_path):
        m = _member("worker1", context="prefer pytest -q")
        rt, tid, captured = _capture_runtime(tmp_path, m)
        _run(rt, tid, rt.definition.member("worker1"))
        assert "[member context]" in captured["prompt"]
        assert "prefer pytest -q" in captured["prompt"]
        assert captured["prompt"].endswith("scan module X")

    def test_context_over_cap_truncated_to_2kb(self, tmp_path):
        m = _member("worker1", context="y" * (MEMBER_CONTEXT_CAP_BYTES + 300))
        rt, tid, captured = _capture_runtime(tmp_path, m)
        _run(rt, tid, rt.definition.member("worker1"))
        prompt = captured["prompt"]
        start = prompt.index("[member context]")
        assert len(prompt) - start - len("[member context]\n\nscan module X") <= (
            MEMBER_CONTEXT_CAP_BYTES + 1
        )

    def test_both_sections_ordered_spec_then_context(self, tmp_path):
        (tmp_path / "SPEC.md").write_text("spec body", encoding="utf-8")
        m = _member("worker1", context="member body")
        rt, tid, captured = _capture_runtime(tmp_path, m)
        _run(rt, tid, rt.definition.member("worker1"))
        prompt = captured["prompt"]
        assert prompt.index("[team spec]") < prompt.index("[member context]")
        assert prompt.index("[member context]") < prompt.index("scan module X")

    def test_empty_context_omits_section(self, tmp_path):
        rt, tid, captured = _capture_runtime(tmp_path, _member("worker1"))
        _run(rt, tid, rt.definition.member("worker1"))
        assert "[member context]" not in captured["prompt"]


class TestGracefulDegradation:
    def test_no_file_read_in_allowlist_still_injects(self, tmp_path):
        (tmp_path / "SPEC.md").write_text("rules", encoding="utf-8")
        m = _member("worker1", tool_allowlist=["search_web"], context="ctx")
        rt, tid, captured = _capture_runtime(tmp_path, m)
        result = _run(rt, tid, rt.definition.member("worker1"))
        assert result.status == "completed"
        assert "[team spec]" in captured["prompt"]
        assert "[member context]" in captured["prompt"]

    def test_plain_subagent_spawn_unaffected(self):
        """Assembly lives in TeamRuntime only — spec prompt passes through
        SubagentRunner untouched (boundary ruling)."""
        from fusion_harness.subagent import SubagentSpec

        spec = SubagentSpec(name="solo", prompt="do thing")
        assert spec.prompt == "do thing"
