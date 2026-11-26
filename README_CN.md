---
doc_id: fusion-harness/readme-cn
product: fusion-harness
class: platform
entry: developer
doc_type: readme
title: { zh: "fusion-harness 总览", en: "fusion-harness" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
---

# fusion-harness（中文版）

Fusion 生态的规范 agent 内核（harness kernel）：预算漏斗、生命周期钩子、权限引擎与上下文压缩。纯标准库实现，零依赖。

## 消费方

- **fusion-agent-studio** 与 **fusion-cowork** 直接 import 本包（硬依赖 —— `pip install -e fusion-harness`；不再保留仓内 fallback 副本）。
- **fusion-code**（TypeScript）维护自己的实现，通过 parity 测试对齐同一份[内核契约](docs/contract.md) —— 目标不是跨语言共享运行时，而是契约一致。

所有运行时的行为契约见 [docs/contract.md](docs/contract.md)。架构决策以 ADR 记录；参见 [ADR 0001](docs/adr-0001-composition-unit.md)：组合单元保持 service 级别，拒绝进程内 everything-is-a-plugin，内核承诺三项已采纳的纪律（事件日志、分段管线、fail-closed 审批）。

## 模块

- `fusion_harness.funnels` — `LLMFunnel`、`ToolFunnel`、`LifecycleBus`（预算执行 + 生命周期事件）
- `fusion_harness.permissions` — `PermissionEngine`（deny-first 权限检查）+ `ConfirmationPolicy` 分层与确定性的 `score_tool_risk()` 风险分档，在引擎内部求值（issue #9）
- `fusion_harness.hooks` — `HookEngine`（生命周期钩子注册表 + 分发）
- `fusion_harness.compactor` — `Compactor`（带保留策略的上下文窗口压缩）
- `fusion_harness.pipeline` — `ToolPipeline` 五段式工具执行：pre-execute 瀑布、受身份保护的单调守卫、一次性 fail-closed 审批、around-dispatch 超时/重试、post-execute 瀑布 —— 最终产出冻结的 `ToolResult`（issue #5）
- `fusion_harness.session_log` — `SessionEventLog` 持久化 append-only、带 schema 版本、可回放的会话事件日志；运行中的 `LifecycleEvent` 会被结构性拒绝（issue #4）
- `fusion_harness.provenance` — `ProvenanceTracker` 对工具结果打 CaMeL 风格污点标签；出站带污点参数在权限引擎之后仅允许窄口径放行（issue #97）
- `fusion_harness.policy_engine` — `PolicyEngine` 声明式 policy-as-artifact（Progent 规则 DSL）；deny-wins 栈式组合、检查点血缘继承、`policy_diff`（issue #98）
- `fusion_harness.consolidation` — `Consolidator` 主动式 FOLD/DROP/REWRITE 动词（可回滚 fold、保留列表安全的 drop、有界 rewrite）（issue #99）
- `fusion_harness.memory_consolidation` — `MemoryConsolidator` 在记忆写入口的 ADD/UPDATE/DELETE/NOOP 操作集；基于墓碑的遗忘（issue #100）
- `fusion_harness.team_topology` — `TOPOLOGY_BANK` + `TopologySelector` 逐步通信拓扑调度（在预算/fanout 约束下权衡角色匹配与 token 成本）（issue #101）
- `fusion_harness.team_runtime` — `TeamRuntime` 生命周期状态机、任务看板、邮箱；通过 `FUSION_TEAM_TOPOLOGY=1` 显式启用拓扑调度（issues #31、#101）
- `evals/compact_bench.py` + `evals/memory_bench.py` — 面向压缩、consolidation 与记忆遗忘的离线回归门禁

## 安全默认（0.2.0，破坏性变更）

权限执行**默认 fail-closed**：未接线 `PermissionEngine` 的 `ToolFunnel` 会拒绝所有工具调用。Headless/CI 部署需显式退出：

```bash
export FUSION_FUNNEL_PERMISSIONS=off   # 或 FUSION_FUNNEL_REQUIRE_PERMISSIONS=0
```

每次以此方式跳过的调用都会记录 `permission enforcement DISABLED via ...` —— 不存在静默解除武装的部署。完整环境变量表见 [docs/contract.md](docs/contract.md) §5。

## 安装

从 PyPI：

```bash
pip install fusion-harness
```

从源码（开发）：

```bash
pip install -e ".[test]"
```

发布由 CI 在 tag push 时（`.github/workflows/release.yml`）通过 PyPI trusted publishing 完成。

## 用法

```python
from fusion_harness import Compactor, LLMFunnel, LifecycleBus, PermissionEngine, ToolFunnel
```

## 测试

```bash
pytest tests/        # 按模块的单元覆盖：funnels、permissions、hooks、compactor
```

测试套件即契约的可执行形态：危险模式拒绝、分层规则优先级、fail-closed 钩子语义、压缩级别、以及执行默认值的翻转。消费方仓库的 CI（agent-studio、cowork）是隐藏 passthrough 假设的下游探测器。

## 许可证

与 fusion monorepo 相同的许可证。
