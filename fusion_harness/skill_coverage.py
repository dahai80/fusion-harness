"""Instruction-level skill coverage labeling (issue #182, C3).

fusion-skills#13 C3 (SkillPlumber, arXiv 2611.25429): skill-evolution
budget must target real shortfalls, not agent non-compliance. This module
labels each skill instruction observed in a run as follow / not-follow /
not-covered and exposes per-instruction counts — the upstream dependency
for the VISE and TeleTune revision paths (fusion-skills DESIGN.md C2).

Deterministic, no model calls: an instruction is surfaced when its
normalized text appears in the run's records (injected block or message
payloads); follow additionally requires action evidence (the declared
tool appears in tool_call events, or the instruction text reaches tool
args/results). Everything else keeps the incumbent label. Pure stdlib.

Consumers read tap-jsonl run logs through the normative trajectory
reader (legacy shapes adapted); cross-task distillation into a shared
strategy library stays consumer-side (fusion-skills owns it).
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from fusion_harness.trajectory import TrajectoryReader

logger = logging.getLogger(__name__)

__all__ = [
    "COVERAGE_LABELS",
    "SkillCoverageError",
    "label_instruction",
    "analyze_run",
    "analyze_tap_jsonl",
    "CoverageLedger",
]

COVERAGE_LABELS = ("follow", "not-follow", "not-covered")

_MAX_TEXT_SNIPPET = 20000


class SkillCoverageError(ValueError):
    """Instruction record or coverage input violates the labeling contract."""


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _validate_instruction(instruction: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(instruction, dict):
        raise SkillCoverageError("instruction must be a dict")
    iid = str(instruction.get("instruction_id") or "").strip()
    text = str(instruction.get("text") or "").strip()
    if not iid or not text:
        raise SkillCoverageError(
            "instruction requires non-empty instruction_id and text: %r" % (sorted(instruction),)
        )
    return {
        "instruction_id": iid,
        "text": text,
        "tool": str(instruction.get("tool") or "").strip(),
    }


def _record_text(record: Any) -> str:
    parts: List[str] = []
    payload = getattr(record, "payload", None)
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (dict, list)):
                parts.append(str(value))
    return _norm_text(" ".join(parts))[:_MAX_TEXT_SNIPPET]


def _run_text(records: Iterable[Any], extra_context: str = "") -> str:
    chunks = [extra_context]
    for record in records:
        event = getattr(record, "event", "")
        if event in ("start", "model_call_meta"):
            continue
        chunks.append(_record_text(record))
    return _norm_text(" ".join(chunks))


def _action_evidence(instruction: Dict[str, Any], records: List[Any]) -> bool:
    tool = instruction["tool"]
    if tool:
        for record in records:
            if getattr(record, "event", "") == "tool_call":
                name = str((getattr(record, "payload", {}) or {}).get("tool") or "")
                if name == tool:
                    return True
        return False
    text = _norm_text(instruction["text"])
    for record in records:
        if getattr(record, "event", "") in ("tool_call", "tool_result"):
            if text and text in _record_text(record):
                return True
    return False


def label_instruction(
    instruction: Dict[str, Any], records: Iterable[Any], extra_context: str = ""
) -> str:
    """Label one instruction against one run: not-covered when never
    surfaced, follow when surfaced + action evidence, not-follow when
    surfaced without action evidence."""
    inst = _validate_instruction(instruction)
    run_text = _run_text(records, extra_context)
    text = _norm_text(inst["text"])
    if not text or text not in run_text:
        return "not-covered"
    return "follow" if _action_evidence(inst, list(records)) else "not-follow"


def analyze_run(
    instructions: Iterable[Dict[str, Any]],
    records: Iterable[Any],
    extra_context: str = "",
) -> Dict[str, str]:
    """Per-instruction labels for one run ({instruction_id: label})."""
    record_list = list(records)
    return {
        _validate_instruction(inst)["instruction_id"]: label_instruction(
            inst, record_list, extra_context
        )
        for inst in instructions
    }


def analyze_tap_jsonl(
    path: str,
    instructions: Iterable[Dict[str, Any]],
    extra_context_by_run: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, str]]:
    """Label every run in a tap-jsonl trajectory log (TrajectoryReader
    semantics; corrupt lines skipped loudly when skip_corrupt). Returns
    {run_id: {instruction_id: label}}."""
    inst_list = [_validate_instruction(inst) for inst in instructions]
    if not inst_list:
        raise SkillCoverageError("no instructions to label")
    reader = TrajectoryReader(path, skip_corrupt=True)
    by_run: Dict[str, List[Any]] = {}
    for record in reader.stream():
        by_run.setdefault(record.run_id, []).append(record)
    if reader.skipped:
        logger.warning("tap jsonl analysis skipped %d corrupt lines", reader.skipped)
    extra = extra_context_by_run or {}
    return {
        run_id: analyze_run(inst_list, records, extra.get(run_id, ""))
        for run_id, records in sorted(by_run.items())
    }


class CoverageLedger:
    """Per-instruction coverage counts across runs — the evolution-budget
    view (C3: target only labeled shortfalls, never agent non-compliance)."""

    def __init__(self) -> None:
        self._counts: Dict[str, Dict[str, int]] = {}

    def observe(self, labels: Dict[str, str]) -> None:
        for iid, label in labels.items():
            if label not in COVERAGE_LABELS:
                raise SkillCoverageError(
                    "unknown coverage label: %s (allowed: %s)" % (label, "|".join(COVERAGE_LABELS))
                )
            bucket = self._counts.setdefault(
                iid, {"follow": 0, "not-follow": 0, "not-covered": 0, "total": 0}
            )
            bucket[label] += 1
            bucket["total"] += 1

    def counts(self) -> Dict[str, Dict[str, int]]:
        return {iid: dict(bucket) for iid, bucket in sorted(self._counts.items())}

    def shortfall_ids(self) -> List[str]:
        """Instructions worth evolution spend (C3): labeled shortfalls only —
        surfaced but not followed at least once. Never-covered instructions
        are a retrieval/injection problem, not a skill defect."""
        return [iid for iid, bucket in sorted(self._counts.items()) if bucket["not-follow"] > 0]
