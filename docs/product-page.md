---
doc_id: fusion-harness/product-page
product: fusion-harness
class: platform
entry: developer
doc_type: product-page
title: { zh: "fusion-harness 产品页", en: "fusion-harness product page" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - docs/contract.md
  - fusion_harness/__init__.py
---

# fusion-harness

```yaml
slug: harness
name: fusion-harness
tagline:
  zh: agent 内核共享库：预算漏斗、权限引擎、生命周期钩子、上下文压缩，纯标准库零依赖
  en: Shared agent kernel library: budget funnels, permission engine, lifecycle hooks, and context compaction — pure stdlib, zero dependencies
cta:
  zh: 十行代码给你的 agent 加上 fail-closed 工具权限
  en: Add fail-closed tool permissions to your agent in ten lines
repo: https://github.com/dahai80/fusion-harness
```

## 平台边界

```yaml
platform:
  serves: 需要 agent 循环的应用（fusion-agent-studio、fusion-cowork）与对齐实现（fusion-code/TS）
  builds_on: 无（Python 标准库；消费方自带 LLM 客户端）
  hosts: 工具调用管道、预算控制、权限判定、会话事件日志
  integration_surface: 进程内 Python API（pip install -e fusion-harness 硬依赖）+ 跨语言行为契约
  extension_points:
    - 自定义 PermissionRule（按层叠规则，deny 优先）
    - 工具 executor 由消费方注入，内核不绑定具体工具集
    - LifecycleBus 钩子由消费方注册
```

## capabilities

```yaml
capabilities:
  - icon: 🛡
    name:
      zh: 权限引擎
      en: Permission engine
    tag:
      zh: 应用方不用自己实现危险命令拦截
      en: Apps skip implementing their own dangerous-command interception
    points:
      - zh: 12 类内置危险模式内容扫描，独立于任何规则生效
        en: 12 built-in dangerous-pattern content scans, effective independent of rules
      - zh: 分层规则（global/node/user），deny 跨层压过 allow
        en: Layered rules (global/node/user); deny beats allow across layers
      - zh: 约束：引擎未武装时工具放行但打 UNARMED 日志，生产必须显式武装或显式关闭
        en: Constraint: an unarmed engine passes tools through with an UNARMED log; production must arm it or disable it explicitly
    viz:
      - ["内置危险模式", "12"]
  - icon: 🪣
    name:
      zh: 预算漏斗
      en: Budget funnels
    tag:
      zh: LLM 调用与工具调用共用一套预算口径
      en: One budget ledger shared by LLM calls and tool calls
    points:
      - zh: LLMFunnel pre/post 两端校验，ToolFunnel 执行前校验
        en: LLMFunnel checks pre/post; ToolFunnel checks before execution
      - zh: 约束：预算 ref 由消费方提供与计费，内核只做判定不做统计
        en: Constraint: the caller owns accounting via budget_ref; the kernel only decides
    viz: []
  - icon: 🧬
    name:
      zh: 上下文压缩
      en: Context compaction
    tag:
      zh: 长会话自动降级，应用方不写截断逻辑
      en: Long sessions degrade automatically; apps write no truncation logic
    points:
      - zh: 分级压缩（reactive_strip → summarize），带 token 估算
        en: Leveled compaction (reactive_strip then summarize) with token estimation
      - zh: 约束：摘要质量取决于消费方注入的 summarize 回调
        en: Constraint: summary quality depends on the caller-injected summarize callback
    viz: []
  - icon: 📜
    name:
      zh: 跨语言契约
      en: Cross-language contract
    tag:
      zh: TS 实现按同一契约对齐，行为不漂移
      en: The TS implementation conforms to the same contract, so behavior does not drift
    points:
      - zh: docs/contract.md 是权威行为契约，测试套件即其可执行形式
        en: docs/contract.md is the authoritative behavior contract; the test suite is its executable form
      - zh: 约束：跨语言只保证契约一致，不共享运行时
        en: Constraint: cross-language means contract conformance, not a shared runtime
    viz: []
```

## workflow

```yaml
workflow:
  - name:
      zh: 安装
      en: Install
    action:
      zh: pip install -e fusion-harness（硬依赖，不允许复制源码内联）
      en: pip install -e fusion-harness (hard dependency; no in-tree copies)
    input:
      zh: Python ≥ 3.11 环境
      en: Python ≥ 3.11 environment
    output:
      zh: fusion_harness 可导入
      en: fusion_harness importable
  - name:
      zh: 武装权限
      en: Arm permissions
    action:
      zh: 构造 PermissionEngine，加规则或依赖内置扫描
      en: Construct a PermissionEngine, add rules or rely on built-in scans
    input:
      zh: 规则清单或默认模式
      en: Rule list or default mode
    output:
      zh: armed 引擎（无 UNARMED 日志）
      en: Armed engine (no UNARMED log)
  - name:
      zh: 接入漏斗
      en: Wire funnels
    action:
      zh: ToolFunnel/LLMFunnel 包住应用的工具执行与 LLM 调用
      en: Wrap the app's tool execution and LLM calls in ToolFunnel/LLMFunnel
    input:
      zh: executor 回调、budget_ref
      en: Executor callbacks, budget_ref
    output:
      zh: 每次调用经过校验→权限→审计
      en: Every call passes validation, permission, and audit
  - name:
      zh: 长会话维护
      en: Long-session upkeep
    action:
      zh: Compactor.should_compact 触发 compact
      en: Trigger compact via Compactor.should_compact
    input:
      zh: 消息历史
      en: Message history
    output:
      zh: 受控长度的上下文
      en: Context kept within controlled length
```

## useCases

```yaml
useCases:
  - name:
      zh: agent-studio / cowork 内嵌内核
      en: Embedded kernel in agent-studio and cowork
    headline:
      zh: 两个应用共享同一权限与预算实现，不再各自维护分支
      en: Two apps share one permission and budget implementation instead of maintaining forks
    body:
      zh: 应用原来各自实现工具放行逻辑；现在统一 `pip install -e fusion-harness`，内核升级即两应用同时生效。差别：安全语义只有一份权威实现。
      en: The apps each maintained their own tool-pass logic; both now install the kernel the same way and kernel upgrades land everywhere at once. Difference: one authoritative safety semantic.
  - name:
      zh: fusion-code 契约对齐
      en: Contract conformance for fusion-code
    headline:
      zh: TS 侧按契约测试对齐，安全行为与 Python 内核一致
      en: The TS side aligns via contract tests, keeping safety behavior identical to the Python kernel
    body:
      zh: TS 运行时无法直接复用 Python 包；改为以 contract.md 为权威、parity 测试锁行为。差别：跨语言行为一致且可回归验证。
      en: The TS runtime cannot import the Python package; contract.md is authoritative and parity tests lock behavior. Difference: consistent cross-language behavior with regression coverage.
```

## techDepth

```yaml
techDepth:
  - category:
      zh: 可靠性纪律
      en: Reliability disciplines
    items:
      - name: { zh: 事件日志, en: Event log }
        desc: { zh: SessionEventLog 记录会话事件，schema 校验失败显式报错, en: SessionEventLog records session events; schema violations raise explicitly }
        steps:
          - zh: 见 fusion_harness/session_log.py
            en: See fusion_harness/session_log.py
      - name: { zh: fail-closed 钩子, en: Fail-closed hooks }
        desc: { zh: 钩子异常按拒绝处理而非吞掉, en: Hook exceptions are treated as denials, never swallowed }
        steps:
          - zh: 见 fusion_harness/hooks.py
            en: See fusion_harness/hooks.py
```

## 提交检查清单

- [x] 所有字段 zh/en 成对
- [x] 每条能力回答"应用方少写了什么"
- [x] integration_surface 与仓库实际一致（pip 硬依赖 + 契约对齐，来自 README）
- [x] extension_points 真实存在（PermissionRule/executor/HookEngine 均为公开 API）
- [x] 数字有出处（12 类危险模式来自 tests/test_permissions.py 的契约断言）
