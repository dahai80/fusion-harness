"""Tests for skill retrieval + prompt injection (#174).

The fusion-trainer contract is consumed, not forked. CI installs this
repo standalone (no fusion_trainer), so retrieval-logic tests run
against a fake `fusion_trainer.skills` module implementing the same
contract shape; one conformance test against the real module is guarded
by importorskip and runs wherever the shared venv has fusion-trainer.
"""

import sys
import types

import pytest

from fusion_harness import (
    LifecycleBus,
    SkillRetrievalError,
    SkillRetriever,
    used_skills_entry,
)


BLOCK_HEADER = "Proven procedural skills (successfully used before — follow when applicable):"


def _fake_render(skills, max_skills=5, max_chars=2000):
    lines = [BLOCK_HEADER]
    for skill in skills[:max_skills]:
        lines.append(
            "- %s (used %sx, verified by outcome_signal)"
            % (skill.get("skill_id", ""), skill.get("success_count", 0))
        )
    return "\n".join(lines)[:max_chars]


def _fake_load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def _install_fake_skills(monkeypatch, render=None):
    fake_trainer = types.ModuleType("fusion_trainer")
    skills_mod = types.ModuleType("fusion_trainer.skills")
    skills_mod.load_skill_library = _fake_load
    skills_mod.render_skill_block = render or _fake_render
    fake_trainer.skills = skills_mod
    monkeypatch.setitem(sys.modules, "fusion_trainer", fake_trainer)
    monkeypatch.setitem(sys.modules, "fusion_trainer.skills", skills_mod)
    return skills_mod


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


def test_render_block_uses_injected_contract(monkeypatch):
    _install_fake_skills(monkeypatch)
    retriever = SkillRetriever(library=_library())
    block = retriever.render_block(retriever.retrieve(task_id="sheet-9"))
    assert block.startswith(BLOCK_HEADER)
    assert "sheet_import (used 3x" in block


def test_render_block_respects_max_chars(monkeypatch):
    _install_fake_skills(
        monkeypatch, render=lambda s, max_skills=5, max_chars=2000: "x" * max_chars
    )
    retriever = SkillRetriever(library=_library(), max_chars=10)
    assert len(retriever.render_block(_library())) == 10


def test_inject_empty_when_no_match():
    retriever = SkillRetriever(library=_library())
    assert retriever.inject(task_id="ghost") == ""


def test_load_missing_library_raises():
    with pytest.raises(SkillRetrievalError):
        SkillRetriever(library_path="/nonexistent/skills.jsonl")


def test_load_requires_path_or_library():
    with pytest.raises(SkillRetrievalError):
        SkillRetriever()


def test_missing_trainer_module_raises_typed_error(monkeypatch):
    # sys.modules[name] = None makes any import of it raise ImportError,
    # deterministically simulating a standalone install without trainer.
    monkeypatch.setitem(sys.modules, "fusion_trainer.skills", None)
    retriever = SkillRetriever(library=_library())
    with pytest.raises(SkillRetrievalError):
        retriever.render_block(_library())
    with pytest.raises(SkillRetrievalError):
        SkillRetriever(library_path="/tmp/any.jsonl")


def test_injection_failure_returns_empty_and_emits_event():
    seen = []
    bus = LifecycleBus()
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        seen.append((event, payload))
        return orig(source, event, payload, session_id=session_id)

    bus.emit = spy
    retriever = SkillRetriever(library=_library(), bus=bus)
    retriever._library = [object()]
    assert retriever.inject(task_id="x") == ""
    assert [e for e, _ in seen if e == "skill_injection_failed"]


def test_inject_emits_event_with_skill_ids(monkeypatch):
    seen = []
    bus = LifecycleBus()
    orig = bus.emit

    def spy(source, event, payload, session_id=""):
        seen.append((event, payload))
        return orig(source, event, payload, session_id=session_id)

    bus.emit = spy
    _install_fake_skills(monkeypatch)
    retriever = SkillRetriever(library=_library(), bus=bus)
    retriever.inject(task_id="sheet-9")
    injected = [p for e, p in seen if e == "skill_block_injected"]
    assert len(injected) == 1
    assert injected[0]["skill_ids"] == ["sheet_import"]


def test_used_skills_entry_for_feedback_loop():
    record = used_skills_entry(["create_document", "", "sheet_import"])
    assert record == {"used_skills": ["create_document", "sheet_import"]}


def test_real_trainer_contract_conformance():
    pytest.importorskip("fusion_trainer")
    from fusion_trainer.skills import render_skill_block

    retriever = SkillRetriever(library=_library())
    skills = retriever.retrieve(task_id="sheet-9")
    real = render_skill_block(skills, max_skills=5, max_chars=2000)
    assert real.startswith("Proven procedural skills")
    assert "used 3x" in real
