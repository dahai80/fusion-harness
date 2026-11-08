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
from fusion_harness.outbox import (
    Effect,
    OutboxStore,
    ReceiptReplayed,
    ReconcileReport,
)

KERNEL_VERSION = "0.4.0"

__all__ = [
    "KERNEL_VERSION",
    "CompactionConfig",
    "Compactor",
    "ConfirmationPolicy",
    "Effect",
    "HookConfig",
    "HookEngine",
    "HookResult",
    "LLMFunnel",
    "LifecycleBus",
    "LogSchemaError",
    "MonotonicGuard",
    "OutboxStore",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRule",
    "PipelineBlocked",
    "PipelineOutcome",
    "ReceiptReplayed",
    "ReconcileReport",
    "SessionEvent",
    "SessionEventLog",
    "SessionState",
    "ToolFunnel",
    "ToolPipeline",
    "ToolResult",
    "score_tool_risk",
]
