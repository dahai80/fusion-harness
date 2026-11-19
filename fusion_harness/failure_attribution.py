"""Failure attribution analyzer (issue #93; arXiv:2505.00212 Who&When,
arXiv:2503.13657 MAST).

Post-hoc analysis over trajectory.py / session_log.py records. Emits
(agent, step, mast_mode) verdicts per run: a coarse pass localizes
candidate failure windows (error events, repeated identical tool calls,
long steps), a pinpoint pass names the decisive step — via an injected
pinpointer (LLM, mockable) or deterministic keyword heuristics.

The 14 MAST modes are a typed regression vocabulary (not free text), so
verdicts aggregate across runs and consumers. Report is plain dicts —
json-able for cowork / agent-studio run views. Offline: zero model calls
unless the consumer injects a live pinpointer.
"""

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class MASTMode(str, Enum):
    """MAST 14 failure modes (arXiv:2503.13657), pinned vocabulary."""

    UNCLEAR_GOALS = "unclear_goals"
    UNCLEAR_ROLES = "unclear_roles"
    INTER_AGENT_DEV_MISALIGNMENT = "inter_agent_dev_misalignment"
    INCORRECT_INFO_TRANSFER = "incorrect_info_transfer"
    INFO_OMISSION = "info_omission"
    INCORRECT_INFO_RETENTION = "incorrect_info_retention"
    FLAWED_REASONING_ON_INFO = "flawed_reasoning_on_info"
    WRONG_AGENT_SELECTED = "wrong_agent_selected"
    INCORRECT_TASK_DECOMPOSITION = "incorrect_task_decomposition"
    RAGGED_MODALITIES = "ragged_modalities"
    PREMATURE_TERMINATION = "premature_termination"
    INCOMPLETE_VERIFICATION = "incomplete_verification"
    WEAK_ACCEPTANCE_CRITERIA = "weak_acceptance_criteria"
    UNVERIFIED_ACCEPTANCE = "unverified_acceptance"


# Cluster grouping per the paper's three top-level categories.
MAST_CLUSTERS: Dict[str, tuple] = {
    "specification_system_design": (
        MASTMode.UNCLEAR_GOALS,
        MASTMode.UNCLEAR_ROLES,
        MASTMode.INTER_AGENT_DEV_MISALIGNMENT,
    ),
    "inter_agent_misalignment": (
        MASTMode.INCORRECT_INFO_TRANSFER,
        MASTMode.INFO_OMISSION,
        MASTMode.INCORRECT_INFO_RETENTION,
        MASTMode.FLAWED_REASONING_ON_INFO,
        MASTMode.WRONG_AGENT_SELECTED,
        MASTMode.INCORRECT_TASK_DECOMPOSITION,
        MASTMode.RAGGED_MODALITIES,
    ),
    "task_verification": (
        MASTMode.PREMATURE_TERMINATION,
        MASTMode.INCOMPLETE_VERIFICATION,
        MASTMode.WEAK_ACCEPTANCE_CRITERIA,
        MASTMode.UNVERIFIED_ACCEPTANCE,
    ),
}


def cluster_of(mode: MASTMode) -> str:
    for cluster, modes in MAST_CLUSTERS.items():
        if mode in modes:
            return cluster
    return "unclustered"


# Deterministic keyword -> mode mapping for the no-LLM pinpoint pass.
_MODE_KEYWORDS = {
    MASTMode.INCORRECT_INFO_TRANSFER: ("contradict", "stale context", "wrong value"),
    MASTMode.INFO_OMISSION: ("missing", "omitted", "not provided", "empty payload"),
    MASTMode.INCORRECT_INFO_RETENTION: ("lost track", "forgot", "stale state"),
    MASTMode.FLAWED_REASONING_ON_INFO: ("invalid", "bad logic", "miscalculat"),
    MASTMode.WRONG_AGENT_SELECTED: ("wrong agent", "misrouted", "not my task"),
    MASTMode.INCORRECT_TASK_DECOMPOSITION: ("decomposition", "subtask", "wrong split"),
    MASTMode.PREMATURE_TERMINATION: ("budget", "stop early", "gave up", "aborted"),
    MASTMode.INCOMPLETE_VERIFICATION: ("unverified", "not verified", "skipped check"),
    MASTMode.UNCLEAR_GOALS: ("unclear goal", "ambiguous task", "no objective"),
    MASTMode.UNCLEAR_ROLES: ("unclear role", "who owns", "no owner"),
}


@dataclass
class FailureVerdict:
    run_id: str
    agent: str
    step: int
    mode: str
    cluster: str
    confidence: float
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "step": self.step,
            "mode": self.mode,
            "cluster": self.cluster,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


@dataclass
class _Candidate:
    step: int
    agent: str
    reason: str
    weight: float = 1.0


def _payload_text(record: Any) -> str:
    payload = getattr(record, "payload", None) or {}
    if not isinstance(payload, dict):
        return str(payload)
    parts = [str(payload.get("error", "")), str(payload.get("status", ""))]
    for key in ("detail", "reason", "message"):
        if payload.get(key):
            parts.append(str(payload[key])[:200])
    return " ".join(parts).lower()


def _args_key(record: Any) -> str:
    payload = getattr(record, "payload", None) or {}
    if not isinstance(payload, dict):
        return ""
    blob = "%s|%s" % (payload.get("tool", ""), payload.get("args", ""))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class FailureAnalyzer:
    """Two-stage attribution over normalized trajectory records.

    pinpointer: optional callable(list_of_records, candidates) ->
    (step_index, mode, confidence, evidence). When absent, the
    deterministic keyword heuristic pinpoints inside the coarse window.
    """

    def __init__(self, pinpointer: Optional[Callable] = None) -> None:
        self.pinpointer = pinpointer

    def analyze(self, records: List[Any]) -> Dict[str, Any]:
        by_run: Dict[str, List[Any]] = defaultdict(list)
        for rec in records:
            by_run[getattr(rec, "run_id", "") or "unknown"].append(rec)
        runs = {}
        for run_id, recs in sorted(by_run.items()):
            recs = sorted(recs, key=lambda r: getattr(r, "ts", 0))
            runs[run_id] = self._analyze_run(run_id, recs)
        aggregate_mode = Counter()
        aggregate_cluster = Counter()
        for run in runs.values():
            for v in run["verdicts"]:
                aggregate_mode[v["mode"]] += 1
                aggregate_cluster[v["cluster"]] += 1
        return {
            "runs": runs,
            "mode_counts": dict(aggregate_mode),
            "cluster_counts": dict(aggregate_cluster),
        }

    def _analyze_run(self, run_id: str, recs: List[Any]) -> Dict[str, Any]:
        candidates = self._coarse(recs)
        verdicts: List[FailureVerdict] = []
        if not candidates:
            logger.info("failure attribution run=%s no candidates", run_id)
            return {"verdicts": [], "responsible_agent": ""}
        if self.pinpointer is not None:
            step, mode, confidence, evidence = self.pinpointer(recs, candidates)
            agent = getattr(recs[step], "node_id", "") if 0 <= step < len(recs) else ""
            try:
                mode_name = mode if isinstance(mode, str) else mode.value
                cluster = cluster_of(MASTMode(mode_name))
            except ValueError:
                logger.warning("pinpointer returned unknown mode %r — mapped to other", mode)
                mode_name = MASTMode.FLAWED_REASONING_ON_INFO.value
                cluster = cluster_of(MASTMode.FLAWED_REASONING_ON_INFO)
            verdicts.append(
                FailureVerdict(
                    run_id=run_id,
                    agent=agent,
                    step=step,
                    mode=mode_name,
                    cluster=cluster,
                    confidence=float(confidence),
                    evidence=str(evidence)[:200],
                )
            )
        else:
            best = max(candidates, key=lambda c: c.weight)
            mode = self._classify(recs, best)
            verdicts.append(
                FailureVerdict(
                    run_id=run_id,
                    agent=best.agent,
                    step=best.step,
                    mode=mode.value,
                    cluster=cluster_of(mode),
                    confidence=min(1.0, 0.4 + 0.2 * best.weight),
                    evidence=best.reason,
                )
            )
        responsible = verdicts[0].agent if verdicts else ""
        return {
            "verdicts": [v.to_dict() for v in verdicts],
            "responsible_agent": responsible,
        }

    @staticmethod
    def _coarse(recs: List[Any]) -> List[_Candidate]:
        candidates: List[_Candidate] = []
        seen_args: Dict[str, int] = {}
        for idx, rec in enumerate(recs):
            agent = getattr(rec, "node_id", "") or getattr(rec, "member", "") or "workflow"
            if getattr(rec, "event", "") == "error":
                candidates.append(_Candidate(idx, agent, "error event", weight=3.0))
                continue
            text = _payload_text(rec)
            if any(word in text for word in ("fail", "denied", "timeout", "exception")):
                candidates.append(
                    _Candidate(idx, agent, "failure signal: %s" % text[:80], weight=2.0)
                )
            if getattr(rec, "event", "") == "tool_call":
                key = _args_key(rec)
                if key and key in seen_args:
                    candidates.append(
                        _Candidate(
                            idx,
                            agent,
                            "repeated identical tool call (first at step %d)" % seen_args[key],
                            weight=1.5,
                        )
                    )
                else:
                    seen_args[key] = idx
        return candidates

    @staticmethod
    def _classify(recs: List[Any], candidate: _Candidate) -> MASTMode:
        rec = recs[candidate.step]
        text = _payload_text(rec) + " " + candidate.reason
        for mode, keywords in _MODE_KEYWORDS.items():
            if any(word in text for word in keywords):
                return mode
        if getattr(rec, "event", "") == "error":
            return MASTMode.FLAWED_REASONING_ON_INFO
        return MASTMode.INCORRECT_INFO_TRANSFER


__all__ = [
    "MAST_CLUSTERS",
    "MASTMode",
    "FailureAnalyzer",
    "FailureVerdict",
    "cluster_of",
]
