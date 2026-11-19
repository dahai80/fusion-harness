"""Cache-warming payback gate (issue #96; pi-mono cacheWarming).

Closes the decision loop from KV/prefix cache telemetry (CacheTelemetry,
v0.22.0 #76) to idle warming action: warm a prompt prefix only when the
expected saving beats the warming cost.

Payback model (deterministic, no model calls):
- cold re-prefill cost  = prefix_tokens * COLD_PREFILL_RATE
- warming cost          = prefix_tokens * WARM_RATE (a cheap prefill ping)
- expected saving       = reuse_rate * cold_cost - warm_cost, where
  reuse_rate is the observed prefix reuse rate from telemetry
  (PrefixStabilityTracker / CacheTelemetry hit rate)

Gate: warm only when expected saving > WARM_MARGIN * warm_cost. The
kernel only DECIDES and emits a typed event per decision (warm/skip +
reason) — executing the ping is the serving host's job (fusion-mlx);
no direct provider calls from the kernel.

Offline: everything runs on synthetic telemetry series.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Cost model constants (per token, relative units — hosts calibrate).
COLD_PREFILL_RATE = 1.0
WARM_RATE = 0.3
# Warm only when expected saving is at least this multiple of warm cost.
WARM_MARGIN = 2.0
MIN_REUSE_RATE = 0.2
# Below this prefix size, warming bookkeeping costs more than it saves.
MIN_PREFIX_TOKENS = 256


@dataclass
class PrefixTelemetry:
    """One observation of a warmable prefix (host-supplied, from its own
    KV/prefix telemetry or the kernel's CacheTelemetry snapshot)."""

    prefix_id: str
    prefix_tokens: int
    reuse_rate: float
    ttl_seconds_remaining: float = 0.0

    def __post_init__(self) -> None:
        if not self.prefix_id:
            raise ValueError("prefix_id required")
        if self.prefix_tokens < 0:
            raise ValueError("prefix_tokens must be >= 0")
        self.reuse_rate = max(0.0, min(1.0, float(self.reuse_rate)))


@dataclass
class WarmDecision:
    prefix_id: str
    warm: bool
    reason: str
    expected_saving: float = 0.0
    warm_cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prefix_id": self.prefix_id,
            "warm": self.warm,
            "reason": self.reason,
            "expected_saving": round(self.expected_saving, 6),
            "warm_cost": round(self.warm_cost, 6),
        }


class CacheWarmingGate:
    """Cost-based decision gate over a telemetry series. Holds no provider
    connections: decide() is pure, observe_and_decide() is the loop entry
    hosts call with each telemetry batch."""

    def __init__(
        self,
        cold_prefill_rate: float = COLD_PREFILL_RATE,
        warm_rate: float = WARM_RATE,
        warm_margin: float = WARM_MARGIN,
        min_reuse_rate: float = MIN_REUSE_RATE,
        min_prefix_tokens: int = MIN_PREFIX_TOKENS,
    ) -> None:
        self.cold_prefill_rate = cold_prefill_rate
        self.warm_rate = warm_rate
        self.warm_margin = warm_margin
        self.min_reuse_rate = min_reuse_rate
        self.min_prefix_tokens = min_prefix_tokens
        self.decisions: List[WarmDecision] = []

    def decide(self, telemetry: PrefixTelemetry) -> WarmDecision:
        cold_cost = telemetry.prefix_tokens * self.cold_prefill_rate
        warm_cost = telemetry.prefix_tokens * self.warm_rate
        expected_saving = telemetry.reuse_rate * cold_cost - warm_cost
        if telemetry.prefix_tokens < self.min_prefix_tokens:
            decision = WarmDecision(
                telemetry.prefix_id, False, "prefix below minimum size", expected_saving, warm_cost
            )
        elif telemetry.reuse_rate < self.min_reuse_rate:
            decision = WarmDecision(
                telemetry.prefix_id,
                False,
                "reuse rate below floor",
                expected_saving,
                warm_cost,
            )
        elif expected_saving > self.warm_margin * warm_cost:
            decision = WarmDecision(
                telemetry.prefix_id, True, "payback exceeds margin", expected_saving, warm_cost
            )
        else:
            decision = WarmDecision(
                telemetry.prefix_id, False, "payback below margin", expected_saving, warm_cost
            )
        self.decisions.append(decision)
        logger.info(
            "cache warming decision prefix=%s warm=%s reason=%s saving=%.1f cost=%.1f",
            telemetry.prefix_id,
            decision.warm,
            decision.reason,
            expected_saving,
            warm_cost,
        )
        return decision

    def observe_and_decide(self, batch: List[PrefixTelemetry]) -> List[WarmDecision]:
        return [self.decide(t) for t in batch]

    def warm_list(self) -> List[str]:
        return [d.prefix_id for d in self.decisions if d.warm]

    def snapshot(self) -> Dict[str, Any]:
        warm = sum(1 for d in self.decisions if d.warm)
        return {
            "decisions": len(self.decisions),
            "warm": warm,
            "skip": len(self.decisions) - warm,
            "warm_list": self.warm_list(),
        }


__all__ = [
    "COLD_PREFILL_RATE",
    "MIN_PREFIX_TOKENS",
    "MIN_REUSE_RATE",
    "WARM_MARGIN",
    "WARM_RATE",
    "CacheWarmingGate",
    "PrefixTelemetry",
    "WarmDecision",
]
