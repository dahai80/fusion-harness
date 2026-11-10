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
from fusion_harness.provider import (
    AdapterHost,
    ProviderAdapter,
    RuntimeNotReady,
    SubprocessSessionRuntime,
    SessionEvent as ProviderSessionEvent,
    SessionState as ProviderSessionState,
)
from fusion_harness.outbox import (
    Effect,
    OutboxStore,
    ReceiptReplayed,
    ReconcileReport,
)
from fusion_harness.policies import (
    Deadline,
    DeadlineExceeded,
    ReceiptCache,
    RetryPolicy,
    TokenLedger,
    TransientError,
    coalesce_retry,
    idempotency_key,
    run_with_retry,
)

KERNEL_VERSION = "0.6.0"

__all__ = [
    "KERNEL_VERSION",
    "CompactionConfig",
    "Compactor",
    "ConfirmationPolicy",
    "Deadline",
    "DeadlineExceeded",
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
    "ProviderAdapter",
    "ProviderSessionEvent",
    "ProviderSessionState",
    "AdapterHost",
    "SubprocessSessionRuntime",
    "RuntimeNotReady",
    "ReceiptCache",
    "ReceiptReplayed",
    "ReconcileReport",
    "RetryPolicy",
    "SessionEvent",
    "SessionEventLog",
    "SessionState",
    "TokenLedger",
    "ToolFunnel",
    "ToolPipeline",
    "ToolResult",
    "TransientError",
    "coalesce_retry",
    "idempotency_key",
    "run_with_retry",
    "score_tool_risk",
]
