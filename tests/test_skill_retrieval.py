"""Tests for skill retrieval + prompt injection (#174)."""

import sys
import types

import pytest

from fusion_harness import (
    LifecycleBus,
    SkillRetrievalError,
    SkillRetriever,
    used_skills_entry,
)


def _install_fake_trainer(monkeypatch):
    # CI has no fusion_trainer installed; the harness consumes its contract
    # lazily, so stub the module to keep render tests offline-deterministic
    mod = types.ModuleType("fusion_trainer")
    skills = types.ModuleType("fusion_trainer.skills")

    def render_skill_block(skills_list, max_skills=5, max_chars=2000):
        parts = [
            "%s (used %dx)" % (s["skill_id"], s.get("success_count", 0))
            for s in skills_list[:max_skills]
        ]
        return "Proven procedural skills: " + ", ".join(parts)

    skills.render_skill_block = render_skill_block
    mod.skills = skills
    monkeypatch.setitem(sys.modules, "fusion_trainer", mod)
    monkeypatch.setitem(sys.modules, "fusion_trainer.skills", skills)


def _library():
    return [
        {
            "schema_version": 2,
            "skill_id": "create_document",
            "tools": ["doc_create"],
            "chain": [],
            "success_count": 1,
            "task_ids": ["doc-report-1"],
            "evidence": [],
            "example_rollout_id": "r1",
            "admission": {},
        },
        {
            "schema_version": 2,
            "skill_id": "sheet_import",
            "tools": ["xlsx_import", "export_xlsx"],
            "chain": ["sheet_import", "export_xlsx"],
            "success_count": 3,
            "task_ids": ["sheet-9"],
            "evidence": [],
            "example_rollout_id": "r2",
            "admission": {},
        },
        {
            "schema_version": 2,
            "skill_id": "web_scrape",
            "tools": ["web_fetch"],
            "chain": [],
            "success_count": 2,
            "task_ids": [],
            "evidence": [],
            "example_rollout_id": "r3",
            "admission": {},
        },
    ]


def test_retrieve_task_id_match_first():
    retriever = SkillRetriever(library=_library())
    picked = retriever.retrieve(task_id="Sheet-9")
    assert [s["skill_id"] for s in picked] == ["sheet_import"]


def test_retrieve_task_id_normalized():
    retriever = SkillRetriever(library=_library())
    assert retriever.retrieve(task_id="  DOC-REPORT-1 ")[0]["skill_id"] == "create_document"


def test_retrieve_tool_overlap_fallback():
    retriever = SkillRetriever(library=_library())
    picked = retriever.retrieve(toolset=["web_fetch", "shell"])
    assert [s["skill_id"] for s in picked] == ["web_scrape"]


def test_retrieve_overlap_prefers_higher_success_count():
    lib = _library()
    lib[2]["success_count"] = 9
    retriever = SkillRetriever(library=lib)
    picked = retriever.retrieve(toolset=["web_fetch", "doc_create"])
    assert [s["skill_id"] for s in picked] == ["web_scrape", "create_document"]


def test_retrieve_no_match_returns_empty():
    retriever = SkillRetriever(library=_library())
    assert retriever.retrieve(task_id="ghost", toolset=["quantum"]) == []


def test_retrieve_caps_at_max_skills():
    retriever = SkillRetriever(library=_library(), max_skills=1)
    picked = retriever.retrieve(toolset=["web_fetch", "doc_create"])
    assert len(picked) == 1


def test_render_block_single_sourced_format(monkeypatch):
    _install_fake_trainer(monkeypatch)
    retriever = SkillRetriever(library=_library())
    block = retriever.render_block(retriever.retrieve(task_id="sheet-9"))
    assert block.startswith("Proven procedural skills")
    assert "used 3x" in block


def test_inject_empty_when_no_match():
    retriever = SkillRetriever(library=_library())
    assert retriever.inject(task_id="ghost") == ""


def test_load_missing_library_raises():
    with pytest.raises(SkillRetrievalError):
        SkillRetriever(library_path="/nonexistent/skills.jsonl")


def test_load_requires_path_or_library():
    with pytest.raises(SkillRetrievalError):
        SkillRetriever()


def test_injection_failure_fails_visible_returns_empty_block():
    bus = LifecycleBus()

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    retriever = SkillRetriever(library=_library(), bus=bus)
    retriever._library = [Broken()]
    assert retriever.inject(task_id="x") == ""


def test_inject_emits_event_with_skill_ids(monkeypatch):
    _install_fake_trainer(monkeypatch)
    seen = []
    bus = LifecycleBus()
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        seen.append((event, payload))
        return orig(source, event, payload, session_id=session_id)

    bus.emit = spy
    retriever = SkillRetriever(library=_library(), bus=bus)
    retriever.inject(task_id="sheet-9")
    injected = [p for e, p in seen if e == "skill_block_injected"]
    assert len(injected) == 1
    assert injected[0]["skill_ids"] == ["sheet_import"]


def test_used_skills_entry_for_feedback_loop():
    record = used_skills_entry(["create_document", "", "sheet_import"])
    assert record == {"used_skills": ["create_document", "sheet_import"]}
