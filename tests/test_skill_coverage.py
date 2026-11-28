"""Tests for instruction-level skill coverage labeling (issue #182, C3/C4)."""

import json

import pytest

from fusion_harness import (
    COVERAGE_LABELS,
    CoverageLedger,
    SkillCoverageError,
    analyze_run,
    analyze_tap_jsonl,
    injection_audit_entry,
)
from fusion_harness.skill_retrieval import SkillRetriever
from fusion_harness.trajectory import TrajectoryWriter

INSTRUCTIONS = [
    {
        "instruction_id": "i1",
        "text": "always verify the checksum before writing",
        "tool": "file_write",
    },
    {"instruction_id": "i2", "text": "use streaming mode for large payloads"},
    {"instruction_id": "i3", "text": "never visible in this run"},
]


def _records(tool=None, args_text=""):
    records = []
    if tool:
        records.append(_rec("tool_call", {"tool": tool, "args": args_text}))
    elif args_text:
        records.append(_rec("tool_result", {"output": args_text}))
    return records


class _Rec:
    def __init__(self, event, payload):
        self.run_id = "r1"
        self.event = event
        self.payload = payload


def _rec(event, payload):
    return _Rec(event, payload)


def test_labels_all_three_labels():
    labels = analyze_run(
        INSTRUCTIONS,
        _records(tool="file_write", args_text="checksum verified then write"),
        extra_context="skill block: always verify the checksum before writing; "
        "use streaming mode for large payloads",
    )
    assert labels["i1"] == "follow"
    assert labels["i2"] == "not-covered" or labels["i2"] in COVERAGE_LABELS


def test_not_covered_when_never_surfaced():
    labels = analyze_run(INSTRUCTIONS, _records(tool="file_write"))
    assert labels["i3"] == "not-covered"


def test_not_follow_when_surfaced_without_action_evidence():
    labels = analyze_run(
        [
            {
                "instruction_id": "i1",
                "text": "always verify the checksum before writing",
                "tool": "file_write",
            }
        ],
        _records(tool="unrelated_tool"),
        extra_context="always verify the checksum before writing",
    )
    assert labels["i1"] == "not-follow"


def test_follow_via_tool_result_text_match():
    labels = analyze_run(
        [{"instruction_id": "i2", "text": "use streaming mode for large payloads"}],
        _records(args_text="operator note: use streaming mode for large payloads"),
        extra_context="use streaming mode for large payloads",
    )
    assert labels["i2"] == "follow"


def test_instruction_validation():
    with pytest.raises(SkillCoverageError):
        analyze_run([{"text": "no id"}], [])
    with pytest.raises(SkillCoverageError):
        analyze_run([{"instruction_id": "x"}], [])
    with pytest.raises(SkillCoverageError):
        analyze_run(["not a dict"], [])


def test_ledger_counts_and_shortfalls():
    ledger = CoverageLedger()
    ledger.observe({"i1": "follow", "i2": "not-follow"})
    ledger.observe({"i1": "not-follow"})
    ledger.observe({"i3": "not-covered"})
    counts = ledger.counts()
    assert counts["i1"] == {"follow": 1, "not-follow": 1, "not-covered": 0, "total": 2}
    assert counts["i3"]["not-covered"] == 1
    assert ledger.shortfall_ids() == ["i1", "i2"]


def test_ledger_rejects_unknown_label():
    with pytest.raises(SkillCoverageError):
        CoverageLedger().observe({"i1": "skipped"})


def test_analyze_tap_jsonl(tmp_path):
    path = str(tmp_path / "tap.jsonl")
    writer = TrajectoryWriter(path)
    writer.write_event(
        "run-a", "tool_call", node_id="n", payload={"tool": "file_write", "args": "checksum"}
    )
    writer.write_event(
        "run-b", "tool_call", node_id="n", payload={"tool": "file_write", "args": "checksum"}
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    report = analyze_tap_jsonl(
        path,
        [{"instruction_id": "i1", "text": "verify the checksum", "tool": "file_write"}],
        extra_context_by_run={
            "run-a": "skill: verify the checksum",
            "run-b": "skill: verify the checksum",
        },
    )
    assert report["run-a"]["i1"] == "follow"
    assert report["run-b"]["i1"] == "follow"


def test_analyze_tap_jsonl_requires_instructions(tmp_path):
    path = str(tmp_path / "tap.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": "r", "node_id": "n", "event": "start"}) + "\n")
    with pytest.raises(SkillCoverageError):
        analyze_tap_jsonl(path, [])


def _fake_library():
    return [
        {
            "skill_id": "s1",
            "task_ids": ["t1"],
            "tools": [],
            "success_count": 3,
            "body": "irrelevant",
        }
    ]


def test_injection_audit_event_emitted(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "fusion_trainer", None)
    import sys as _sys

    fake_trainer = type(_sys)("fusion_trainer")
    fake_skills = type(_sys)("fusion_trainer.skills")
    fake_skills.load_skill_library = lambda path: _fake_library()
    fake_skills.render_skill_block = lambda skills, max_skills=5, max_chars=2000: (
        "skill s1 block text"
    )
    fake_trainer.skills = fake_skills
    monkeypatch.setitem(_sys.modules, "fusion_trainer", fake_trainer)
    monkeypatch.setitem(_sys.modules, "fusion_trainer.skills", fake_skills)

    events = []

    class _Bus:
        def emit(self, kind, event, payload, session_id=""):
            events.append((event, payload))

    retriever = SkillRetriever(library=_fake_library(), bus=_Bus())
    block = retriever.inject(task_id="t1")
    assert block
    audit_events = [p for e, p in events if e == "skill_injection_audit"]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit["task_id"] == "t1"
    assert audit["skill_ids"] == ["s1"]
    assert audit["format_source"] == "fusion_trainer.skills.render_skill_block"
    assert audit["injection_point"] == "system-prefix"
    assert audit["block_chars"] == len(block)
    assert retriever.last_injection_audit == audit


def test_injection_audit_entry_defaults():
    entry = injection_audit_entry(task_id="t", skill_ids=["s"], max_skills=5, max_chars=100)
    assert entry["injection_point"] == "system-prefix"
    assert entry["block_chars"] == 0
    assert entry["skill_ids"] == ["s"]
