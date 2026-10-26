"""fusion-harness — canonical agent harness kernel (funnels/hooks/permissions/compactor).

Extracted from fusion-agent-studio N0 kernel (#357). Pure stdlib, no deps.
Consumers: fusion-agent-studio (shim import), fusion-cowork, fusion-code.
"""

from fusion_harness.compactor import CompactionConfig, Compactor
from fusion_harness.funnels import LifecycleBus, LLMFunnel, ToolFunnel
from fusion_harness.hooks import HookConfig, HookEngine, HookResult
from fusion_harness.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

KERNEL_VERSION = "0.2.1"

__all__ = [
    "KERNEL_VERSION",
    "CompactionConfig",
    "Compactor",
    "HookConfig",
    "HookEngine",
    "HookResult",
    "LLMFunnel",
    "LifecycleBus",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRule",
    "ToolFunnel",
]
