"""Compact-Bench-style offline A/B evaluation gate for compactor changes
(issue #72; arXiv 2610.31710, 2610.29869, 2611.03223).

Method: each fixture is a recorded run transcript. The same task state is
scored twice — variant A with full history, variant B with the compacted
state produced by the compactor under test — against a deterministic
stub model. The stub is a context-availability oracle (not an LLM): a
task marker or pinned rule can only be honored if it is still present in
the context, so any A/B delta isolates compaction damage from model noise
with zero model calls. Deterministic and CI-safe.

Metrics per fixture:
- task-success: all task markers still available in variant B.
- rule-survival recall: rules pinned outside the compaction region
  (system prompt, ARCBench-style) must still be honored after compaction.
- tokens saved: estimate_tokens(full) - estimate_tokens(compacted).

Gate: fail the build when task success drops or rule recall falls below
threshold. One seeded known-bad summarizer fixture proves the gate can
fail (a summarizer that drops critical rules must not pass silently).
"""

import json
import logging
import os
from typing import Any, Dict, List

from fusion_harness.tokens import HeuristicTokenCounter, count_messages

logger = logging.getLogger(__name__)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

DEFAULT_MIN_RULE_RECALL = 1.0


class EvalGateError(AssertionError):
    """A/B gate failed: compaction damage beyond threshold."""


def load_fixtures(directory: str = FIXTURES_DIR) -> List[Dict[str, Any]]:
    fixtures = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                fixtures.append(json.load(fh))
    return fixtures


def _text_of(messages: List[Dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def stub_score(messages: List[Dict[str, Any]], fixture: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic context-availability oracle standing in for the model:
    task success = every task marker still present; rule recall = fraction
    of pinned rules still present verbatim."""
    text = _text_of(messages)
    missing_markers = [m for m in fixture.get("task_markers", []) if m not in text]
    rules = fixture.get("rules", [])
    missing_rules = [r for r in rules if r not in text]
    recall = (len(rules) - len(missing_rules)) / len(rules) if rules else 1.0
    return {
        "success": not missing_markers,
        "missing_markers": missing_markers,
        "rule_recall": round(recall, 6),
        "missing_rules": missing_rules,
    }


def run_fixture(
    compactor: Any,
    fixture: Dict[str, Any],
    level: str = "warning",
) -> Dict[str, Any]:
    """Variant A (full history) vs variant B (compacted by the compactor
    under test) on one recorded transcript."""
    full = list(fixture["messages"])
    compacted = compactor.compact(list(full), level)
    score_a = stub_score(full, fixture)
    score_b = stub_score(compacted, fixture)
    counter = HeuristicTokenCounter()
    tokens_full = count_messages(full, counter)
    tokens_compacted = count_messages(compacted, counter)
    result = {
        "name": fixture["name"],
        "variant_a": score_a,
        "variant_b": score_b,
        "tokens_full": tokens_full,
        "tokens_compacted": tokens_compacted,
        "tokens_saved": tokens_full - tokens_compacted,
        "task_success_delta": bool(score_b["success"]) - bool(score_a["success"]),
        "rule_recall": score_b["rule_recall"],
    }
    logger.info(
        "compact-bench fixture=%s success_a=%s success_b=%s recall=%.3f saved=%d",
        result["name"],
        score_a["success"],
        score_b["success"],
        result["rule_recall"],
        result["tokens_saved"],
    )
    return result


def run_suite(
    compactor: Any,
    fixtures: List[Dict[str, Any]],
    level: str = "warning",
) -> Dict[str, Any]:
    results = [run_fixture(compactor, f, level) for f in fixtures]
    failed = [r["name"] for r in results if not _passes(r)]
    # Issue #88: compaction-failure telemetry — refusals/empty outputs that
    # hit the degraded fallback path, as a rate over LLM compaction calls.
    # Duck-typed: non-Compactor stand-ins simply report no LLM calls.
    stats = getattr(compactor, "failure_stats", None)
    failure_rate = 0.0
    if stats and stats.get("llm_calls", 0) > 0:
        failure_rate = round(stats["fallbacks"] / stats["llm_calls"], 6)
    return {
        "fixtures": results,
        "failed": failed,
        "passed": not failed,
        "compaction_failures": dict(stats) if stats else {},
        "compaction_failure_rate": failure_rate,
    }


def _passes(result: Dict[str, Any], min_rule_recall: float = DEFAULT_MIN_RULE_RECALL) -> bool:
    return bool(result["variant_b"]["success"]) and result["rule_recall"] >= min_rule_recall


def assert_gate(report: Dict[str, Any], min_rule_recall: float = DEFAULT_MIN_RULE_RECALL) -> None:
    """CI gate: any task-success drop or rule-recall below threshold fails
    the build with the offending fixtures named."""
    bad = []
    for r in report["fixtures"]:
        if not _passes(r, min_rule_recall):
            bad.append(
                "%s (success=%s recall=%.3f missing_rules=%s)"
                % (
                    r["name"],
                    r["variant_b"]["success"],
                    r["rule_recall"],
                    r["variant_b"]["missing_rules"],
                )
            )
    if bad:
        raise EvalGateError("compact-bench gate failed: " + "; ".join(bad))


class BadSummarizer:
    """Seeded known-bad summarizer (issue #72): simulates a compaction
    change that silently drops pinned rules and the retention header —
    the exact Compaction-Laundering failure mode. Must FAIL the gate."""

    def compact(
        self, messages: List[Dict[str, Any]], level: str = "warning"
    ) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            content = str(m.get("content") or "")
            lines = [
                ln
                for ln in content.splitlines()
                if "constraint:" not in ln.lower() and not ln.startswith("[Retention")
            ]
            if m.get("role") == "system" and (
                "constraint:" in content.lower() or content.startswith("[Retention")
            ):
                continue
            out.append({**m, "content": "\n".join(lines)})
        return out


def run_consolidation_fixture(
    consolidator: Any,
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """Consolidation axis (issue #99): apply the fixture's FOLD/DROP
    annotations through the consolidator under test, then score marker
    survival, rule recall, undo correctness (fold -> rehydrate -> original
    text back) and state-cap compliance (refusal where one is expected).
    """
    from fusion_harness.consolidation import ConsolidationError

    full = list(fixture["messages"])
    consolidated = list(full)
    undo_ok = True
    for start, end, summary in fixture.get("folds", []):
        try:
            consolidated = consolidator.fold(consolidated, start, end, summary)
            restored = consolidator.undo_fold(consolidated, start)
            original_texts = [full[i].get("content") for i in range(start, end)]
            restored_texts = [m.get("content") for m in restored[start : start + (end - start)]]
            if original_texts != restored_texts:
                undo_ok = False
                logger.warning("undo mismatch fixture=%s scope=%s", fixture["name"], [start, end])
        except Exception as e:
            undo_ok = False
            logger.warning("fold/undo failed fixture=%s err=%s", fixture["name"], e)
    for start, end, reason in fixture.get("drops", []):
        try:
            consolidated = consolidator.drop(consolidated, start, end, reason)
        except ConsolidationError:
            pass
    cap_ok = True
    case = fixture.get("rewrite_case")
    if case:
        try:
            consolidator.rewrite(case["prev_state"], case["observation"])
            if case.get("expect_refusal"):
                cap_ok = False
        except ConsolidationError:
            cap_ok = bool(case.get("expect_refusal"))
        except Exception as e:
            logger.warning("rewrite case error fixture=%s err=%s", fixture["name"], e)
            cap_ok = False
    score = stub_score(consolidated, fixture)
    counter = HeuristicTokenCounter()
    tokens_full = count_messages(full, counter)
    tokens_consolidated = count_messages(consolidated, counter)
    result = {
        "name": fixture["name"],
        "success": score["success"],
        "rule_recall": score["rule_recall"],
        "undo_ok": undo_ok,
        "cap_ok": cap_ok,
        "tokens_full": tokens_full,
        "tokens_consolidated": tokens_consolidated,
        "tokens_saved": tokens_full - tokens_consolidated,
    }
    logger.info(
        "consolidation-bench fixture=%s success=%s undo_ok=%s cap_ok=%s recall=%.3f saved=%d",
        result["name"],
        result["success"],
        undo_ok,
        cap_ok,
        result["rule_recall"],
        result["tokens_saved"],
    )
    return result


def run_consolidation_suite(consolidator: Any, fixtures: List[Dict[str, Any]]) -> Dict[str, Any]:
    results = [run_consolidation_fixture(consolidator, f) for f in fixtures]
    failed = [
        r["name"]
        for r in results
        if not (
            r["success"]
            and r["undo_ok"]
            and r["cap_ok"]
            and r["rule_recall"] >= DEFAULT_MIN_RULE_RECALL
        )
    ]
    return {"fixtures": results, "failed": failed, "passed": not failed}


def assert_consolidation_gate(report: Dict[str, Any]) -> None:
    """CI gate for the consolidation axis: any marker loss, rule loss,
    undo failure or state-cap violation names the offending fixture."""
    bad = []
    for r in report["fixtures"]:
        if not (
            r["success"]
            and r["undo_ok"]
            and r["cap_ok"]
            and r["rule_recall"] >= DEFAULT_MIN_RULE_RECALL
        ):
            bad.append(
                "%s (success=%s undo_ok=%s cap_ok=%s recall=%.3f)"
                % (r["name"], r["success"], r["undo_ok"], r["cap_ok"], r["rule_recall"])
            )
    if bad:
        raise EvalGateError("consolidation-bench gate failed: " + "; ".join(bad))


class BadConsolidator:
    """Seeded known-bad consolidator (issue #99): folds ranges into stubs
    that carry no archive link and silently drop pinned rules — the
    consolidation-laundering failure mode. Must FAIL the gate."""

    def fold(self, messages, start, end, summary):
        stub = {"role": "system", "content": "[fold] lossy: %s" % summary}
        return messages[:start] + [stub] + messages[end:]

    def drop(self, messages, start, end, reason):
        return messages[:start] + messages[end:]


__all__ = [
    "BadConsolidator",
    "BadSummarizer",
    "EvalGateError",
    "assert_consolidation_gate",
    "assert_gate",
    "load_fixtures",
    "run_consolidation_fixture",
    "run_consolidation_suite",
    "run_fixture",
    "run_suite",
    "stub_score",
]
