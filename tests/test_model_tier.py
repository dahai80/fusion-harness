"""Issue #141: per-step model-tier vocabulary on TeamMember and SpawnBrief."""

import pytest

from fusion_harness import MODEL_TIERS, VALID_MODEL_TIERS, SpawnBrief, SpawnBriefError
from fusion_harness.team import TeamDefError, TeamDefinition, TeamMember


def _member(name="m1", **kw):
    return TeamMember(name=name, role="worker", **kw)


def _definition(**kw):
    params = dict(
        name="scan-team",
        namespace_key="team:scan-2026-11",
        members=[_member("lead"), _member("worker1")],
    )
    params.update(kw)
    return TeamDefinition(**params)


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


class TestModelTierVocabulary:
    def test_constants(self):
        assert VALID_MODEL_TIERS == ("", "cheap", "strong")
        assert MODEL_TIERS == ("cheap", "strong")

    def test_member_default_unspecified(self):
        assert _member().model_tier == ""

    def test_member_valid_tiers(self):
        assert _member(model_tier="cheap").model_tier == "cheap"
        assert _member(model_tier="strong").model_tier == "strong"

    def test_member_invalid_tier_rejected(self):
        with pytest.raises(TeamDefError, match="unknown model tier 'fast'"):
            _member(model_tier="fast")

    def test_definition_rejects_mutated_invalid_tier(self):
        member = _member("w1")
        member.model_tier = "premium"  # bypass __post_init__ post-construction
        with pytest.raises(TeamDefError, match="unknown model tier"):
            _definition(members=[_member("lead"), member])

    def test_brief_default_unspecified(self):
        assert _brief().model_tier == ""

    def test_brief_valid_tiers(self):
        assert _brief(model_tier="cheap").model_tier == "cheap"
        assert _brief(model_tier="strong").model_tier == "strong"

    def test_brief_invalid_tier_rejected(self):
        with pytest.raises(SpawnBriefError, match="model_tier"):
            _brief(model_tier="medium")
