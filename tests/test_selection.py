"""Selection protocol (issue #186, proposed): eval-gated A/B comparator.
Winner's Curse discipline (arXiv 2610.09239): max-score selection on
noisy small evals picks luck — a promotion needs effect beyond the
incumbent's noise band AND permutation significance; noise-only pairs
must resolve inconclusive. Plus HoldoutSplit enforcing train/holdout
separation at the data layer. Pure stdlib, deterministic given seed."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))

from fusion_harness.selection import HoldoutSplit, compare  # noqa: E402


def test_identical_scores_inconclusive():
    result = compare([0.7, 0.72, 0.69], [0.7, 0.72, 0.69], seed=7)
    assert result["verdict"] == "inconclusive"
    assert result["p"] > 0.05


def test_clear_improvement_keeps():
    incumbent = [0.60, 0.61, 0.59, 0.62, 0.60]
    candidate = [0.85, 0.86, 0.84, 0.87, 0.85]
    result = compare(incumbent, candidate, seed=7)
    assert result["verdict"] == "keep"
    assert result["p"] < 0.05


def test_clear_regression_rolls_back():
    incumbent = [0.85, 0.86, 0.84, 0.87, 0.85]
    candidate = [0.60, 0.61, 0.59, 0.62, 0.60]
    result = compare(incumbent, candidate, seed=7)
    assert result["verdict"] == "rollback"


def test_noise_only_pair_inconclusive():
    incumbent = [0.60, 0.75, 0.55, 0.80, 0.50, 0.70]
    candidate = [0.72, 0.58, 0.78, 0.52, 0.74, 0.60]
    result = compare(incumbent, candidate, seed=7)
    assert result["verdict"] == "inconclusive"


def test_small_effect_within_noise_band_inconclusive():
    incumbent = [0.60, 0.75, 0.55, 0.80, 0.50, 0.70]
    candidate = [0.68, 0.62, 0.75, 0.58, 0.72, 0.65]
    result = compare(incumbent, candidate, seed=7, alpha=0.05)
    assert result["verdict"] == "inconclusive"


def test_report_fields_present():
    result = compare([0.5, 0.6], [0.7, 0.8], seed=7)
    assert set(result) >= {"verdict", "p", "noise_band", "incumbent_mean", "candidate_mean", "delta"}


def test_deterministic_given_seed():
    a = compare([0.5, 0.6, 0.55], [0.65, 0.58, 0.7], seed=42)
    b = compare([0.5, 0.6, 0.55], [0.65, 0.58, 0.7], seed=42)
    assert a == b


def test_empty_scores_inconclusive_not_crash():
    result = compare([], [0.5], seed=7)
    assert result["verdict"] == "inconclusive"
    result = compare([0.5], [], seed=7)
    assert result["verdict"] == "inconclusive"


def test_holdout_split_disjoint_and_deterministic():
    cases = ["c%d" % i for i in range(20)]
    s1 = HoldoutSplit(cases, holdout_frac=0.25, seed=11)
    s2 = HoldoutSplit(cases, holdout_frac=0.25, seed=11)
    assert s1.holdout == s2.holdout
    assert not set(s1.train) & set(s1.holdout)
    assert sorted(s1.train + s1.holdout) == sorted(cases)
    assert len(s1.holdout) == 5


def test_holdout_split_contains_sealed_flag():
    s = HoldoutSplit(["a", "b", "c", "d"], holdout_frac=0.5, seed=3)
    assert s.sealed is True
    assert s.opened is False
    s.open()
    assert s.opened is True


def test_compare_paired_clear_improvement_keeps():
    from fusion_harness.selection import compare_paired

    inc = [0.40, 0.50, 0.45, 0.55, 0.42, 0.48]
    cand = [0.90, 0.95, 0.88, 0.92, 0.91, 0.89]
    result = compare_paired(inc, cand, seed=7)
    assert result["verdict"] == "keep"


def test_compare_paired_noisy_absolute_gap_but_consistent_deltas_keeps():
    """The E1 failure mode: cross-fixture variance swamps the two-sample
    band, but per-fixture deltas are uniformly positive — paired test
    must keep where compare() said inconclusive."""
    from fusion_harness.selection import compare, compare_paired

    inc = [0.30, 0.60, 0.45, 0.75, 0.50, 0.55]
    cand = [0.45, 0.75, 0.60, 0.90, 0.65, 0.70]
    two_sample = compare(inc, cand, seed=7)
    paired = compare_paired(inc, cand, seed=7)
    assert two_sample["verdict"] == "inconclusive"
    assert paired["verdict"] == "keep"


def test_compare_paired_mixed_deltas_inconclusive():
    from fusion_harness.selection import compare_paired

    inc = [0.50, 0.60, 0.70, 0.80]
    cand = [0.55, 0.58, 0.65, 0.95]
    result = compare_paired(inc, cand, seed=7)
    assert result["verdict"] == "inconclusive"


def test_compare_paired_length_mismatch_raises():
    from fusion_harness.selection import compare_paired

    try:
        compare_paired([0.5, 0.6], [0.5], seed=7)
    except ValueError as exc:
        assert "same length" in str(exc)
    else:
        raise AssertionError("mismatched pairs must raise")
