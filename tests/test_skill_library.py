"""Tests for skill library lifecycle — four gates, pruning, routing (issue #54)."""

import os

import pytest

from fusion_harness.skills import (
    Skill,
    SkillCase,
    SkillError,
    SkillLibrary,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
)


def _skill(name="deploy-db", desc="deploy the database safely", body="run migrations then verify"):
    return Skill(name=name, description=desc, body=body)


def _lib(now=1000.0):
    t = {"v": now}
    return SkillLibrary(clock=lambda: t["v"]), t


class TestFourGates:
    def test_happy_path_new_skill(
        self,
    ):
        lib, t = _lib()
        out = lib.propose(
            _skill(),
            provenance="author:opus",
            cases=[SkillCase(must_mention=["migrations"])],
            canary_confirmed=True,
        )
        assert out.name == "deploy-db"
        assert lib.status("deploy-db") == STATUS_ACTIVE

    def test_generation_gate_blocks_missing_provenance(self):
        lib, _ = _lib()
        with pytest.raises(SkillError, match="gate generation"):
            lib.propose(_skill(), provenance="", canary_confirmed=True)

    def test_verification_gate_blocks_failing_case(self):
        lib, _ = _lib()
        with pytest.raises(SkillError, match="gate verification"):
            lib.propose(
                _skill(),
                provenance="a",
                cases=[SkillCase(must_mention=["rollback-procedure"])],
                canary_confirmed=True,
            )

    def test_canary_gate_blocks_unconfirmed_rollout(self):
        lib, _ = _lib()
        with pytest.raises(SkillError, match="gate canary"):
            lib.propose(_skill(), provenance="a")

    def test_rollback_gate_blocks_stale_base(self):
        lib, t = _lib()
        lib.propose(_skill(), provenance="a", canary_confirmed=True)
        t["v"] = 2000.0
        with pytest.raises(SkillError, match="gate rollback"):
            lib.propose(
                _skill(body="v2 body"), provenance="a", base_version=99, canary_confirmed=True
            )

    def test_modification_with_correct_base_accepts_and_versions(self):
        lib, t = _lib()
        lib.propose(_skill(), provenance="a", canary_confirmed=True)
        t["v"] = 2000.0
        lib.propose(_skill(body="v2 body"), provenance="b", base_version=1, canary_confirmed=True)
        rolled = lib.rollback("deploy-db", 1)
        assert rolled.body == "run migrations then verify"
        with pytest.raises(SkillError, match="no such version"):
            lib.rollback("deploy-db", 9)


class TestPruning:
    def test_prune_archives_never_deletes(self):
        lib, t = _lib()
        lib.propose(_skill("cold-skill"), provenance="a", canary_confirmed=True)
        lib.record_usage("cold-skill")
        t["v"] = 1000.0 + 2 * 86400
        archived = lib.prune(min_uses=5, window_s=86400)
        assert archived == ["cold-skill"]
        assert lib.status("cold-skill") == STATUS_ARCHIVED
        assert lib.registry.get("cold-skill").name == "cold-skill"

    def test_active_skill_survives_prune(self):
        lib, t = _lib()
        lib.propose(_skill("hot-skill"), provenance="a", canary_confirmed=True)
        for _ in range(10):
            lib.record_usage("hot-skill")
        t["v"] += 2 * 86400
        assert lib.prune(min_uses=5, window_s=86400) == []
        assert lib.status("hot-skill") == STATUS_ACTIVE

    def test_archived_skill_not_selectable(self):
        lib, t = _lib()
        lib.propose(_skill("old"), provenance="a", canary_confirmed=True)
        t["v"] += 2 * 86400
        lib.prune(min_uses=1, window_s=86400)
        assert all(s.name != "old" for s in lib.select("old migrations"))
        assert lib.status("old") == STATUS_ARCHIVED


class TestRouting:
    def _grown(self, lib):
        skills = [
            _skill("deploy-db", "deploy database migrations", "run migrations"),
            _skill("write-tests", "write unit test suites", "pytest coverage"),
            _skill("seo-audit", "audit seo metadata", "crawl metadata"),
            _skill("payroll-run", "run monthly payroll", "salary accounting"),
            _skill("css-tuning", "tune css styles", "stylesheet polish"),
        ]
        for s in skills:
            lib.propose(s, provenance="a", canary_confirmed=True)

    def test_route_scores_by_task_features(self):
        lib, _ = _lib()
        self._grown(lib)
        top = lib.select("fix failing pytest coverage in unit test suites", top_k=1)
        assert top[0].name == "write-tests"

    def test_route_beats_flat_retrieval_noise(self):
        lib, _ = _lib()
        self._grown(lib)
        task = "deploy database migrations"
        routed = lib.select(task, top_k=1)
        flat_names = lib.registry.names()
        assert routed[0].name == "deploy-db"
        assert flat_names[0] != "deploy-db"
        assert len(lib.select(task, top_k=3)) == 3

    def test_route_empty_task_returns_stable_order(self):
        lib, _ = _lib()
        self._grown(lib)
        out = lib.select("", top_k=2)
        assert len(out) == 2


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "skills.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
