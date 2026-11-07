---
doc_id: fusion-harness/api-reference
product: fusion-harness
class: platform
entry: developer
doc_type: api-reference
title: { zh: "fusion-harness API 参考", en: "fusion-harness API reference" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - fusion_harness/__init__.py
  - fusion_harness/funnels.py
  - fusion_harness/permissions.py
  - fusion_harness/compactor.py
  - fusion_harness/hooks.py
  - fusion_harness/pipeline.py
  - fusion_harness/session_log.py
---

# fusion-harness API 参考（API Reference）

> 版本 0.3.0。签名以 `fusion_harness/__init__.py` 的 `__all__` 为准。

## 权限（permissions.py）

### `PermissionEngine`

| 成员 | 签名 | 说明 |
|---|---|---|
| `evaluate` | `(tool_name: str, args: dict) -> PermissionDecision` | 完整判定：内容扫描 → 分层规则 → 模式兜底 |
| `add_rule` | `(rule: PermissionRule, *, layer: str)` | 叠加规则；`deny` 优先于 `allow`，跨层同样 |
| `load_file_rules` | `(path)` | 从 JSON 文件载入规则 |
| `is_armed` | `() -> bool` | 引擎是否已武装（有规则或非默认模式） |
| `passthrough_enabled` | `() -> bool` | 未武装时是否放行 |
| `score_tool_risk` | 模块级函数 | 按工具名/参数估算风险分 |

### `PermissionDecision`

字段：`decision`（`allow` / `deny` / `approve`）、`rule_source`（如 `content_scan`、`rule:global:0`）。

### `PermissionRule`

字段：`action`（`allow` / `deny`）、`tool`（工具名，精确匹配）。构造：`PermissionRule(action="allow", tool="echo_tool")`。

### 危险模式内容扫描

12 类内置模式（正则），任何规则之前生效。例：`rm -rf /`、`curl ... | sh` 均被拒绝，`rule_source == "content_scan"`。清单见 `DANGEROUS_PATTERNS`，数量由 `tests/test_permissions.py` 契约断言锁定。

## 漏斗（funnels.py）

### `ToolFunnel`

| 成员 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(hooks=None, permissions=None, lifecycle_bus=None, budget_ref=None, audit_logger=None, readonly_tools=None, safety_gateway=None)` | 全部可选注入 |
| `run_tool` | `async (ctx, tool_name: str, args: dict, executor, node_label: str = "") -> tuple[str, bool]` | 校验 → 权限 → 执行 → 审计；`executor` 为 `(validated_args) -> awaitable`；返回 `(结果串, 是否错误)` |
| `admit` / `record_result` | — | 拆开用：先判准入，后记结果 |

### `LLMFunnel`

| 成员 | 签名 | 说明 |
|---|---|---|
| `pre_llm` | `(messages, ...)` | 请求端预算/截断校验 |
| `post_llm` | `(response, ...)` | 响应端校验 |
| `set_budget_ref` | `(ref)` | 绑定预算句柄 |

### `LifecycleBus`

生命周期事件广播；注册回调来自消费方。

## 管道（pipeline.py）

| 名称 | 说明 |
|---|---|
| `ToolPipeline` | 多工具编排入口 |
| `PipelineOutcome` / `ToolResult` | 执行结果载体 |
| `PipelineBlocked` | 管道拒绝时抛出 |
| `MonotonicGuard` | 防倒退守卫（状态只许前进） |

## 钩子（hooks.py）

| 名称 | 说明 |
|---|---|
| `HookEngine` | 钩子执行器；**fail-closed**：钩子异常按拒绝处理 |
| `HookConfig` / `HookResult` | 配置与结果载体 |

## 压缩（compactor.py）

| 成员 | 说明 |
|---|---|
| `Compactor.compact(messages)` | 分级压缩主入口 |
| `Compactor.should_compact(...)` | 是否需要压缩的判定 |
| `Compactor.estimate_tokens(...)` | token 估算 |
| `Compactor.reactive_strip(...)` | 低级压缩：剥历史 |
| `Compactor.summarize_messages(...)` | 高级压缩：摘要（依赖注入回调） |
| `CompactionConfig` | 压缩策略配置 |

## 会话日志（session_log.py）

| 名称 | 说明 |
|---|---|
| `SessionEventLog` | 会话事件追加日志 |
| `SessionEvent` / `SessionState` | 事件与状态载体 |
| `LogSchemaError` | schema 违例显式抛出（不静默降级） |

## 版本

`KERNEL_VERSION`（当前 `0.3.0`）随 `__all__` 导出，消费方据此做兼容判断。
