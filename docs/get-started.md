---
doc_id: fusion-harness/get-started
product: fusion-harness
class: platform
entry: developer
doc_type: get-started
title: { zh: "fusion-harness 快速上手", en: "fusion-harness quick start" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - docs/contract.md
  - fusion_harness/__init__.py
---

# fusion-harness 快速上手（Get Started）

> 目标：10 分钟内在你的代码里跑通第一个经过权限漏斗的工具调用。

## 1. 前置条件

- Python ≥ 3.11（纯标准库实现，无第三方依赖）
- 无需任何外部服务

## 2. 安装

```bash
pip install fusion-harness          # PyPI
pip install -e ".[test]"            # 仓库开发态
```

确认点：

```bash
python3 -c "from fusion_harness import KERNEL_VERSION; print(KERNEL_VERSION)"
# 期望输出：0.3.0
```

## 3. 最小示例

```python
import asyncio
from fusion_harness import PermissionEngine, PermissionRule, ToolFunnel

engine = PermissionEngine()
engine.add_rule(PermissionRule(action="allow", tool="echo_tool"), layer="global")
funnel = ToolFunnel(permissions=engine)


async def exec_echo(args):
    return f"echo:{args['text']}"


async def main():
    ok = await funnel.run_tool(None, "echo_tool", {"text": "hi"}, exec_echo)
    denied = await funnel.run_tool(None, "shell", {"command": "rm -rf /"}, exec_echo)
    print(ok)
    print(denied)


asyncio.run(main())
```

期望输出（真实运行结果）：

```text
permission engine is UNARMED (no rules, mode=default): tools pass through. Set FUSION_PERMISSION_MODE or add rules to arm it; set FUSION_PERMISSION_PASSTHROUGH=0 to require approval.
content scan blocked tool=shell pattern=rm\s+-rf\s+/
('echo:hi', False)
('Blocked by permission: dangerous pattern matched: rm\\s+-rf\\s+/', True)
```

要点：`shell` 调用即使没有任何显式规则，也被内置危险模式扫描拒绝（fail-closed 语义）。

## 4. 核心 API 面

| 入口 | 用途 |
|---|---|
| `PermissionEngine.evaluate(tool, args)` | 返回 `PermissionDecision`（allow/deny/approve + rule_source） |
| `PermissionEngine.add_rule(rule, layer=...)` | 按层叠规则，deny 优先于 allow，跨层同样 |
| `ToolFunnel.run_tool(ctx, name, args, executor)` | 校验→权限→执行→审计的完整漏斗，返回 `(result_str, is_error)` |
| `LLMFunnel.pre_llm / post_llm` | 请求/响应两端的预算与截断 |
| `Compactor.compact(messages)` | 上下文压缩（分级策略） |
| `HookEngine` / `LifecycleBus` | 生命周期钩子，fail-closed：钩子异常按拒绝处理 |

## 5. 集成到你的项目

- 集成形态：进程内库，`pip install -e fusion-harness` 硬依赖（agent-studio / cowork 即此模式）
- fusion-code（TypeScript）不共享运行时，只按 [contract](contract.md) 做一致性对齐
- 最常见的坑：权限引擎未武装时（无规则且 mode=default）工具直接放行并打 UNARMED 日志——生产部署必须武装或显式 `FUSION_FUNNEL_PERMISSIONS=off`（会打 DISABLED 日志，不存在静默 unarmed）

## 6. 下一步

- 行为契约（跨运行时权威）：[contract.md](contract.md)
- API 细节：[api-reference.md](api-reference.md)
- 配置（环境变量表）：[config-reference.md](config-reference.md)
- 实战配方：[recipes](recipes/)

## 提交检查清单

- [x] 最小示例真实跑通（本机实际执行，输出原样粘贴）
- [x] API 名与 0.3.0 源码核对（`fusion_harness/__init__.py`）
- [x] 无真实密钥
- [x] zh/en 成对（代码除外）
- [x] 全篇 ≤ 120 行
