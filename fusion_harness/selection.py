"""Selection protocol (issue #186): eval-gated A/B comparator for
self-improvement loops. Winner's Curse discipline (arXiv 2610.09239):
a candidate wins only when its improvement clears the incumbent's noise
band AND survives a two-sided permutation test — max-score selection on
noisy small evals otherwise keeps luck. Pure stdlib, deterministic given
seed (exact permutation when combinatorially small, seeded sampling
otherwise). Never an LLM judge; safe to freeze and hide from proposers.
"""

import logging
import math
import random
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

VERDICTS = ("keep", "rollback", "inconclusive")


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _noise_band(incumbent: Sequence[float], floor: float) -> float:
    """RRSI step-1 rule: band scales with incumbent baseline variance."""
    return max(floor, _stdev(incumbent))


def _perm_p(incumbent: List[float], candidate: List[float], seed: int) -> float:
    """Two-sided permutation p for the mean difference, exact when the
    split space is small (<= 20000), seeded sampled otherwise."""
    pooled = incumbent + candidate
    n_a = len(incumbent)
    observed = abs(_mean(candidate) - _mean(incumbent))
    if not pooled or observed == 0.0:
        return 1.0
    total = math.comb(len(pooled), n_a)
    rng = random.Random(seed)
    count = 0
    trials = 0
    if total <= 20000:
        for idx in combinations(range(len(pooled)), n_a):
            a = [pooled[i] for i in idx]
            b = [pooled[i] for i in range(len(pooled)) if i not in idx]
            trials += 1
            if abs(_mean(b) - _mean(a)) >= observed - 1e-12:
                count += 1
        return count / trials
    trials = 10000
    for _ in range(trials):
        shuffled = pooled[:]
        rng.shuffle(shuffled)
        a = shuffled[:n_a]
        b = shuffled[n_a:]
        if abs(_mean(b) - _mean(a)) >= observed - 1e-12:
            count += 1
    return (count + 1) / (trials + 1)


def compare(
    incumbent_scores: Sequence[float],
    candidate_scores: Sequence[float],
    alpha: float = 0.05,
    band_floor: float = 0.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """Gate one candidate against the incumbent. Verdicts:
    keep (beats band + significant), rollback (loses band + significant),
    inconclusive (either test fails — keep the incumbent)."""
    inc = list(incumbent_scores)
    cand = list(candidate_scores)
    band = _noise_band(inc, band_floor)
    delta = _mean(cand) - _mean(inc) if inc and cand else 0.0
    p = _perm_p(inc, cand, seed) if inc and cand else 1.0
    significant = p < alpha
    if delta > band and significant:
        verdict = "keep"
    elif delta < -band and significant:
        verdict = "rollback"
    else:
        verdict = "inconclusive"
    result = {
        "verdict": verdict,
        "p": p,
        "noise_band": band,
        "incumbent_mean": _mean(inc),
        "candidate_mean": _mean(cand),
        "delta": delta,
    }
    logger.info(
        "selection verdict=%s delta=%.4f band=%.4f p=%.4f",
        verdict,
        delta,
        band,
        p,
    )
    return result


class HoldoutSplit:
    """Deterministic train/holdout split; the holdout is sealed until the
    final open() — Winner's-Curse discipline at the data layer."""

    def __init__(
        self,
        cases: Sequence[str],
        holdout_frac: float = 0.2,
        seed: int = 0,
    ) -> None:
        items = sorted(cases)
        rng = random.Random(seed)
        shuffled = items[:]
        rng.shuffle(shuffled)
        n_hold = max(1, int(round(len(items) * holdout_frac))) if items else 0
        self.holdout = shuffled[:n_hold]
        self.train = shuffled[n_hold:]
        self.sealed = True
        self.opened = False

    def open(self) -> None:
        """Single-use: opening the holdout is a recorded event."""
        if self.opened:
            logger.warning("holdout opened more than once — holdout discipline broken")
        self.opened = True
        self.sealed = False
        logger.info("holdout opened cases=%d", len(self.holdout))


__all__ = ["HoldoutSplit", "VERDICTS", "compare"]
