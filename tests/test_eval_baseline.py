"""Harness evaluation baseline gate (plan P2-4).

Runs the deterministic compaction eval (no LLM) and fails on regression
beyond the tolerance recorded in bench/baseline.json.
"""

import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
)

from bench.compaction_eval import check_regression, load_baseline, run_eval  # noqa: E402

from fusion_harness.compactor import CompactionConfig, Compactor  # noqa: E402

BENCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
TRAJECTORY = os.path.join(BENCH_DIR, "trajectories", "compaction_future_questions.json")
BASELINE = os.path.join(BENCH_DIR, "baseline.json")


def test_compaction_future_questions_baseline():
    trajectory = json.load(open(TRAJECTORY))
    compactor = Compactor(CompactionConfig(keep_recent_rounds=1))
    result = run_eval(trajectory, compactor)
    baseline = load_baseline(BASELINE)
    verdict = check_regression(result, baseline)
    assert verdict["ok"], "%s missed=%s" % (verdict["reason"], result["missed"])
