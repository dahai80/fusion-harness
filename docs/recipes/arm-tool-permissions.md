---
doc_id: fusion-harness/recipes/arm-tool-permissions
product: fusion-harness
class: platform
entry: developer
doc_type: recipe
title: { zh: "配方：给工具漏斗武装权限", en: "Recipe: arm tool-funnel permissions" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - fusion_harness/permissions.py
  - tests/test_permissions.py
---

# 配方：给工具漏斗武装权限（Arm tool-funnel permissions）

场景：agent 应用要在生产环境强制工具权限，且不允许未武装状态悄悄上线。

## 步骤

1. 构造引擎并加规则（deny 优先于 allow，跨层同样）：

```python
from fusion_harness import PermissionEngine, PermissionRule

engine = PermissionEngine()
engine.add_rule(PermissionRule(action="allow", tool="read_file"), layer="global")
engine.add_rule(PermissionRule(action="deny", tool="shell"), layer="global")
```

2. 注入漏斗：

```python
from fusion_harness import ToolFunnel

funnel = ToolFunnel(permissions=engine)
```

3. 启动时断言已武装（防配置回退）：

```python
assert engine.is_armed(), "permission engine must be armed in production"
```

4. 调用路径不用变：`run_tool` 内部先内容扫描、再分层规则；`rm -rf /` 这类参数即使没写 deny 规则也会被内置扫描拒绝。

## 验证

```python
import asyncio

res = asyncio.run(funnel.run_tool(None, "shell", {"command": "rm -rf /"}, exec))
# 期望：('Blocked by permission: dangerous pattern matched: ...', True)
```

## 常见坑

- 只加规则不改模式：引擎 armed 与否看 `is_armed()`，规则+默认模式即可武装；但若环境变量把 `FUSION_FUNNEL_PERMISSIONS` 设成了 `off`，规则不生效，且日志有 `permission enforcement DISABLED via ...` —— 部署检查先查这条日志。
- 规则用 `tool="sh"` 想拦所有 shell 变体：规则是精确匹配，拦变体靠内容扫描，不要靠枚举工具名。
