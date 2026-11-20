"""Deterministic per-phase tier policy (issue #142).

Pure step-kind -> model-tier-hint mapping. No model call, no routing
model in the kernel (router stays consumer-side per RouteLLM's
router-as-component shape): cascade economics (FrugalGPT arXiv:2305.05176,
RouteLLM arXiv:2406.18665) show tool-call/planning steps tolerate cheap
models while synthesis/decision steps need the strong tier. Consumers
resolve the returned hint to an actual model; "" = no recommendation.
"""

TIER_POLICY = {
    "tool_call": "cheap",
    "planning": "cheap",
    "member_execution": "cheap",
    "synthesis": "strong",
    "decision": "strong",
    "verification": "strong",
}

__all__ = ["TIER_POLICY", "recommend_tier"]


def recommend_tier(step_kind: str) -> str:
    """Deterministic tier hint for a step kind; "" when the kind is unknown
    (fail-open to the consumer's default, never a guess)."""
    return TIER_POLICY.get(step_kind, "")
