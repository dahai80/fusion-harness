---
doc_id: fusion-harness/architecture
product: fusion-harness
class: platform
entry: developer
doc_type: architecture
title: { zh: "fusion-harness 架构", en: "fusion-harness architecture" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/contract.md
  - docs/adr-0001-composition-unit.md
  - fusion_harness/
---

# fusion-harness 架构（Architecture）

> 行为契约是权威（[contract.md](contract.md)）；本页讲模块分层与决策记录。
> 组合单元决策见 [ADR 0001](adr-0001-composition-unit.md)：组合单元是服务级，
> 拒绝进程内 everything-is-a-plugin。

## 分层

```text
应用（agent-studio / cowork / code[TS·契约对齐]）
        │ import
┌───────────────────────────────────────────┐
│ fusion_harness（纯标准库，零依赖）        │
│                                           │
│  funnels      LLMFunnel / ToolFunnel /    │  ← 调用边界：校验+预算+审计
│               LifecycleBus                │
│  pipeline     ToolPipeline / Outcome /    │  ← 多工具编排
│               MonotonicGuard              │
│  permissions  PermissionEngine / Rule /   │  ← 判定内核：内容扫描+分层规则
│               Decision                    │
│  hooks        HookEngine（fail-closed）   │  ← 生命周期
│  compactor    Compactor（分级压缩）       │  ← 上下文治理
│  session_log  SessionEventLog（schema 硬校验）│ ← 事件日志
└───────────────────────────────────────────┘
        │ 无外部调用（纯进程内，无网络）
        ▼
消费方注入的 executor / summarize 回调 / budget_ref
```

## 关键决策

| 决策 | 内容 | 依据 |
|---|---|---|
| 组合单元 = 服务级 | 不做进程内插件总线 | ADR 0001 |
| 权限 fail-closed | 0.2.0 起默认强制；无引擎 = 拒绝；显式关闭必留日志 | README Security defaults、contract §5 |
| deny > allow | 内容扫描 → 分层规则 → 模式兜底的顺序，deny 恒优先 | tests/test_permissions.py |
| 契约优先于运行时 | TS 不共享包，只对齐 contract.md 并跑 parity 测试 | README Consumers |
| 纯标准库 | 零第三方依赖，任何消费方零成本引入 | pyproject.toml |

## 一次工具调用的路径

```text
run_tool(ctx, name, args, executor)
  1. 校验与清洗 args        → 不合法即拒
  2. PermissionEngine.evaluate(name, args)
       内容扫描（12 模式，最先）
       分层规则（deny 压 allow）
       模式兜底（armed/approve）
     → deny 则返回 ('Blocked by permission: ...', True)，不触 executor
  3. 预算判定（budget_ref）
  4. await executor(validated_args)
  5. 审计 + 生命周期事件
  → (result_str, is_error)
```

## 演进规则

- 行为变更必须先改 contract.md，再改实现与测试（测试套件 = 契约的可执行形式）
- 消费方（agent-studio / cowork）的 CI 是隐藏透传假设的下游探测器
- 新增危险模式 / 规则层 / 压缩级别：同步更新契约测试数量断言
