"""fusion-harness — canonical agent harness kernel (funnels/hooks/permissions/compactor).

Extracted from fusion-agent-studio N0 kernel (#357). Pure stdlib, no deps.
Consumers: fusion-agent-studio (shim import), fusion-cowork, fusion-code.
"""

from fusion_harness.compactor import CompactionConfig, Compactor
from fusion_harness.funnels import LifecycleBus, LLMFunnel, ToolFunnel
from fusion_harness.pipeline import (
    MonotonicGuard,
    PipelineBlocked,
    PipelineOutcome,
    ToolPipeline,
    ToolResult,
)
from fusion_harness.hooks import HookConfig, HookEngine, HookResult
from fusion_harness.permissions import (
    ConfirmationPolicy,
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    score_tool_risk,
)
from fusion_harness.session_log import (
    LogSchemaError,
    SessionEvent,
    SessionEventLog,
    SessionState,
)

KERNEL_VERSION = "0.3.0"

__all__ = [
    "KERNEL_VERSION",
    "CompactionConfig",
    "Compactor",
    "ConfirmationPolicy",
    "HookConfig",
    "HookEngine",
    "HookResult",
    "LLMFunnel",
    "LifecycleBus",
    "LogSchemaError",
    "MonotonicGuard",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRule",
    "PipelineBlocked",
    "PipelineOutcome",
    "SessionEvent",
    "SessionEventLog",
    "SessionState",
    "ToolFunnel",
    "ToolPipeline",
    "ToolResult",
    "score_tool_risk",
]
