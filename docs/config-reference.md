---
doc_id: fusion-harness/config-reference
product: fusion-harness
class: platform
entry: developer
doc_type: config-reference
title: { zh: "fusion-harness 配置参考", en: "fusion-harness configuration reference" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - docs/contract.md
  - fusion_harness/permissions.py
  - fusion_harness/funnels.py
---

# fusion-harness 配置参考（Configuration Reference）

> 内核无配置文件；运行时配置全部通过环境变量 + 代码内构造参数。
> 环境变量权威表见 [contract.md](contract.md) §5，本页按使用场景归纳。

## 权限相关环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `FUSION_PERMISSION_MODE` | `default` | 权限模式；无规则时引擎处于未武装状态 |
| `FUSION_PERMISSION_PASSTHROUGH` | 开 | 未武装时放行工具调用；设 `0` 改为要求审批 |
| `FUSION_FUNNEL_PERMISSIONS` | on | 总开关；`off`（或 `FUSION_FUNNEL_REQUIRE_PERMISSIONS=0`）显式关闭权限强制 |
| `FUSION_FUNNEL_REQUIRE_PERMISSIONS` | `1` | 同上的互补写法 |

行为约定：

- 未武装（无规则且 mode=default）→ 工具放行，但每次打 `permission engine is UNARMED ...` 日志
- 显式关闭 → 每次跳过打 `permission enforcement DISABLED via ...` 日志
- 两条路径都不存在静默无保护运行；生产部署要么武装引擎，要么留下显式关闭日志

## 代码内配置点

| 配置点 | 位置 | 说明 |
|---|---|---|
| 规则清单 | `PermissionEngine.add_rule(rule, layer=...)` | 层名任意；deny 优先 |
| 文件规则 | `PermissionEngine.load_file_rules(path)` | JSON 规则文件 |
| 工具执行 | `ToolFunnel(executor 回调)` | 消费方注入，内核不绑定工具集 |
| 预算 | `budget_ref` / `LLMFunnel.set_budget_ref` | 计费与统计在消费方 |
| 压缩策略 | `CompactionConfig` | 级别、阈值、摘要回调 |
| 钩子 | `HookConfig` / `LifecycleBus` 注册 | fail-closed 语义 |

## 默认值速查

| 项 | 值 | 出处 |
|---|---|---|
| 内置危险模式数 | 12 | `DANGEROUS_PATTERNS`（测试契约断言） |
| `KERNEL_VERSION` | `0.3.0` | `fusion_harness/__init__.py` |
| Python 下限 | ≥ 3.11 | `pyproject.toml` |
| 第三方依赖 | 0（纯标准库） | `pyproject.toml` |
