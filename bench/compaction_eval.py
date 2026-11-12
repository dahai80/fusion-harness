"""Compaction evaluation baseline (plan P2-4, gap G10).

"Future questions" method (arXiv 2610.27301): score a compaction path on
questions whose answers matter AFTER the compaction point — facts pinned by
the survival contract, retention-extractable rules, tool-result stub
identity, and recent-round content. Deterministic regex matching over the
compacted context (no LLM in CI); MLX-path evaluation stays nightly-side.

Scores persist in bench/baseline.json; the CI test fails on regression
beyond the documented tolerance.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.0


def compacted_text(messages: List[Dict[str, Any]]) -> str:
    parts = []
    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        parts.append(content)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            parts.append(str(fn.get("name", "")))
            parts.append(str(fn.get("arguments", "")))
    return "\n".join(parts)


def run_eval(trajectory: Dict[str, Any], compactor: Any) -> Dict[str, Any]:
    """Compact a trajectory and answer its future questions.

    trajectory: {"name": str, "messages": [...], "questions":
                 [{"id": str, "expect": regex, "level": compaction level}]}
    Returns {"name", "score", "total", "missed": [ids]}.
    """
    messages = trajectory.get("messages") or []
    questions = trajectory.get("questions") or []
    missed: List[str] = []
    checked = 0
    for q in questions:
        level = str(q.get("level") or "warning")
        compacted = compactor.compact(messages, level=level)
        if not re.search(str(q["expect"]), compacted_text(compacted)):
            missed.append(str(q["id"]))
        checked += 1
    score = (checked - len(missed)) / checked if checked else 1.0
    result = {
        "name": str(trajectory.get("name") or "unnamed"),
        "score": round(score, 4),
        "total": checked,
        "missed": missed,
    }
    logger.info("eval finished name=%s score=%.4f missed=%s", result["name"], score, missed)
    return result


def load_baseline(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def check_regression(result: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    """Compare an eval result against the baseline entry by name."""
    entry = baseline.get(result["name"])
    if entry is None:
        return {"ok": True, "reason": "no baseline entry (informational)"}
    tolerance = float(entry.get("tolerance", DEFAULT_TOLERANCE))
    floor = float(entry["score"]) - tolerance
    ok = result["score"] >= floor
    return {
        "ok": ok,
        "reason": "" if ok else "score %.4f below baseline floor %.4f" % (result["score"], floor),
    }


__all__ = ["run_eval", "load_baseline", "check_regression", "compacted_text"]
