"""Per-member privacy routing constraint (biz delivery pipeline).

Tests: TeamMember.privacy field + validation, run_member carries the
constraint verbatim in spec.metadata["privacy"] (kernel stays
model-agnostic; consumers own enforcement).
"""

import asyncio

import pytest

from fusion_harness.spawn_protocol import SpawnBrief
from fusion_harness.team import TeamDefError, TeamDefinition, TeamMember
from fusion_harness.team_runtime import TeamRuntime, TeamTask


def _brief():
    return SpawnBrief(
        objective="do the thing",
        output_format="text",
        tool_guidance="t",
        boundaries="b",
        token_budget=5000,
        concurrency=1,
        depth=1,
    )


def _runtime(tmp_path, member):
    rt = TeamRuntime(
        TeamDefinition(
            name="privacy-team",
            namespace_key="team:privacy-2026-11",
            members=[member],
        ),
        state_dir=str(tmp_path),
    )
    rt.activate()
    tid = rt.add_task(TeamTask(title="t"))
    return rt, tid


class TestMemberPrivacyField:
    def test_default_empty(self):
        assert TeamMember(name="w", role="worker").privacy == ""

    def test_valid_local_only(self):
        m = TeamMember(name="w", role="worker", privacy="local-only")
        assert m.privacy == "local-only"

    def test_invalid_mode_raises(self):
        with pytest.raises(TeamDefError):
            TeamMember(name="w", role="worker", privacy="cloud")


class TestRunMemberCarriesPrivacy:
    def test_privacy_forwarded_to_metadata(self, tmp_path):
        member = TeamMember(name="worker1", role="worker", privacy="local-only")
        rt, tid = _runtime(tmp_path, member)
        captured = {}

        async def agent_fn(spec, session):
            captured["metadata"] = dict(spec.metadata)
            return "done"

        result = asyncio.run(
            rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn)
        )
        assert result.summary == "done"
        assert captured["metadata"]["privacy"] == "local-only"

    def test_no_privacy_no_key(self, tmp_path):
        member = TeamMember(name="worker1", role="worker")
        rt, tid = _runtime(tmp_path, member)
        captured = {}

        async def agent_fn(spec, session):
            captured["metadata"] = dict(spec.metadata)
            return "done"

        asyncio.run(rt.run_member(rt.definition.member("worker1"), tid, _brief(), agent_fn))
        assert "privacy" not in captured["metadata"]
