"""fusion-harness — canonical agent harness kernel (funnels/hooks/permissions/compactor).

Extracted from fusion-agent-studio N0 kernel (#357). Pure stdlib, no deps.
Consumers: fusion-agent-studio (shim import), fusion-cowork, fusion-code.
"""

from fusion_harness.artifact_scan import (
    ArtifactQuarantine,
    ScanReport,
    scan_artifact,
    stamp_untrusted,
)
from fusion_harness.cancellation import (
    CancelToken,
    CancellationRegistry,
    TurnStateError,
)
from fusion_harness.experience import (
    NOTEBOOK_MULTIPLIER,
    GuardVerdict,
    LoopGuard,
    PruneProposal,
    SpiralState,
    ToolPruner,
    is_notebook_tool,
)
from fusion_harness.member_comms import (
    FinishSubscriptionStore,
    archive_cascade,
    detach_member,
    envelope_for,
    extract_envelopes,
    sanitize_human_prompt,
    wrap_envelope,
)
from fusion_harness.permission_request import (
    ACTION_INTENTS,
    REQUEST_KINDS,
    PermissionRequest,
    PermissionRequestError,
    PermissionResponse,
    RequestAction,
    ResolvedRequest,
    resolve_request,
)
from fusion_harness.checkpoint import (
    CheckpointState,
    CheckpointStore,
)
from fusion_harness.terminal_rows import (
    TERMINAL_EVENTS,
    TerminalRowTracker,
    wire_terminal_invariant,
)
from fusion_harness.provider_adapter import (
    EVENT_VARIANTS,
    TOOL_KINDS,
    AdapterEventError,
    AgentCapabilityFlags,
    AgentClient,
    AgentSession,
    AgentStreamEvent,
    ToolCallDetail,
    normalize_tool_kind,
)
from fusion_harness.destructive_gate import ApprovalGate
from fusion_harness.untrusted import INJECTION_PATTERNS, scan_untrusted
from fusion_harness.compactor import (
    CompactionConfig,
    Compactor,
)
from fusion_harness.tokens import (
    CJKAwareHeuristicTokenCounter,
    HeuristicTokenCounter,
    TokenCounter,
    count_messages,
)
from fusion_harness.funnels import (
    CacheTelemetry,
    LifecycleBus,
    LLMFunnel,
    ToolFunnel,
)

from fusion_harness.orchestrator import (
    BackgroundPool,
    ParallelTeam,
)
from fusion_harness.artifact_channel import (
    ArtifactStore,
    SummaryInjector,
    WorkerDelivery,
)
from fusion_harness.cache_discipline import mask_tools
from fusion_harness.playbook import (
    Playbook,
    PlaybookOp,
)
from fusion_harness.spawn_protocol import (
    FANOUT_COMPLEX_MAX,
    FANOUT_HARD_MAX,
    FANOUT_SIMPLE_MAX,
    MODEL_TIERS,
    MULTI_AGENT_TOKEN_MULTIPLIER,
    TOKEN_MULTIPLIER_WARNING,
    SpawnBrief,
    SpawnBriefError,
    brief_from_event,
    fanout_warnings,
    validate_brief,
)
from fusion_harness.team import (
    VALID_MODEL_TIERS,
    TeamDefError,
    TeamDefinition,
    TeamMember,
)
from fusion_harness.tier_policy import TIER_POLICY, recommend_tier
from fusion_harness.scoped_permissions import (
    ScopedPermission,
    ScopedPermissionEngine,
    ScopedPermissionError,
)
from fusion_harness.dag_node import (
    BaseNode,
    NodeRegistry,
    NodeRegistryError,
)
from fusion_harness.checkpoint_binding import bind_node_state
from fusion_harness.artifact_channel import ArtifactTools
from fusion_harness.trajectory import (
    TrajectoryRecord,
    TrajectorySchemaError,
    TrajectoryWriter,
    normalize_record,
)
from fusion_harness.compaction_archive import (
    cite_link,
    extract_links,
)
from fusion_harness.failure_attribution import FailureAnalyzer
from fusion_harness.human_gate import (
    AWAITING_HUMAN_MARKER,
    HumanGate,
)
from fusion_harness.json_repair import (
    JSONRepairError,
    parse_repair,
)
from fusion_harness.team_runtime import (
    MSG_TYPES,
    TeamMessage,
    TeamMessageError,
    TeamRuntime,
    TeamTask,
)
from fusion_harness.sandbox import (
    ExecutionTier,
    SandboxExecRunner,
    SandboxPolicy,
    SandboxRunner,
)
from fusion_harness.executor_backend import (
    ExecutorBackendUnavailable,
    FusionExecutorBackend,
    select_sandbox_runner,
)
from fusion_harness.skills import (
    Skill,
    SkillError,
    SkillRegistry,
    parse_skill,
)
from fusion_harness.subagent import (
    SubagentRunner,
    SubagentSpec,
)
from fusion_harness.pipeline import ToolPipeline
from fusion_harness.hooks import (
    HookConfig,
    HookEngine,
    HookResult,
)
from fusion_harness.permissions import (
    ConfirmationPolicy,
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    score_tool_risk,
)
from fusion_harness.stream_guard import LoopDetector
from fusion_harness.team_topology import (
    TopologyError,
    TopologySelector,
)
from fusion_harness.session_log import SessionEventLog
from fusion_harness.outbox import OutboxStore
from fusion_harness.policies import (
    ReceiptCache,
    RetryPolicy,
    idempotency_key,
)

KERNEL_VERSION = "0.30.0"

__all__ = [
    "KERNEL_VERSION",
    "TERMINAL_EVENTS",
    "TerminalRowTracker",
    "wire_terminal_invariant",
    "CancelToken",
    "CancellationRegistry",
    "TurnStateError",
    "ACTION_INTENTS",
    "REQUEST_KINDS",
    "PermissionRequest",
    "PermissionRequestError",
    "PermissionResponse",
    "RequestAction",
    "ResolvedRequest",
    "resolve_request",
    "NOTEBOOK_MULTIPLIER",
    "GuardVerdict",
    "LoopGuard",
    "PruneProposal",
    "SpiralState",
    "ToolPruner",
    "is_notebook_tool",
    "FinishSubscriptionStore",
    "archive_cascade",
    "detach_member",
    "envelope_for",
    "extract_envelopes",
    "sanitize_human_prompt",
    "wrap_envelope",
    "EVENT_VARIANTS",
    "TOOL_KINDS",
    "AdapterEventError",
    "AgentCapabilityFlags",
    "AgentClient",
    "AgentSession",
    "AgentStreamEvent",
    "ToolCallDetail",
    "normalize_tool_kind",
    "CompactionConfig",
    "Compactor",
    "ArtifactQuarantine",
    "ScanReport",
    "CheckpointState",
    "ApprovalGate",
    "INJECTION_PATTERNS",
    "scan_untrusted",
    "CheckpointStore",
    "CacheTelemetry",
    "ConfirmationPolicy",
    "CJKAwareHeuristicTokenCounter",
    "Skill",
    "SkillError",
    "SkillRegistry",
    "SubagentRunner",
    "BackgroundPool",
    "ParallelTeam",
    "SubagentSpec",
    "parse_skill",
    "HeuristicTokenCounter",
    "TokenCounter",
    "count_messages",
    "FailureAnalyzer",
    "HookConfig",
    "HookEngine",
    "HookResult",
    "LLMFunnel",
    "ExecutionTier",
    "SandboxPolicy",
    "FusionExecutorBackend",
    "ExecutorBackendUnavailable",
    "select_sandbox_runner",
    "SpawnBrief",
    "SpawnBriefError",
    "validate_brief",
    "brief_from_event",
    "fanout_warnings",
    "FANOUT_SIMPLE_MAX",
    "FANOUT_COMPLEX_MAX",
    "FANOUT_HARD_MAX",
    "MULTI_AGENT_TOKEN_MULTIPLIER",
    "TOKEN_MULTIPLIER_WARNING",
    "MODEL_TIERS",
    "ArtifactStore",
    "SummaryInjector",
    "WorkerDelivery",
    "Playbook",
    "PlaybookOp",
    "mask_tools",
    "TeamMember",
    "TeamDefinition",
    "TeamDefError",
    "VALID_MODEL_TIERS",
    "TIER_POLICY",
    "recommend_tier",
    "TeamRuntime",
    "TeamTask",
    "TeamMessage",
    "TeamMessageError",
    "MSG_TYPES",
    "BaseNode",
    "NodeRegistry",
    "NodeRegistryError",
    "bind_node_state",
    "ArtifactTools",
    "TrajectoryRecord",
    "TrajectorySchemaError",
    "TrajectoryWriter",
    "normalize_record",
    "AWAITING_HUMAN_MARKER",
    "HumanGate",
    "ScopedPermission",
    "ScopedPermissionEngine",
    "ScopedPermissionError",
    "SandboxRunner",
    "SandboxExecRunner",
    "LifecycleBus",
    "LoopDetector",
    "TopologyError",
    "TopologySelector",
    "OutboxStore",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRule",
    "ReceiptCache",
    "RetryPolicy",
    "SessionEventLog",
    "ToolFunnel",
    "ToolPipeline",
    "idempotency_key",
    "score_tool_risk",
    "JSONRepairError",
    "parse_repair",
    "scan_artifact",
    "stamp_untrusted",
    "cite_link",
    "extract_links",
]
