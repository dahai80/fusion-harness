---
doc_id: fusion-harness/recipes/compact-long-session
product: fusion-harness
class: platform
entry: developer
doc_type: recipe
title: { zh: "配方：压缩超长会话上下文", en: "Recipe: compact an over-long session" }
audience: developer
status: draft
version: 0.3.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - fusion_harness/compactor.py
  - tests/test_compactor.py
---

# 配方：压缩超长会话上下文（Compact an over-long session）

场景：agent 会话消息数持续增长，需要在逼近上下文上限前主动降级。

## 步骤

1. 构造 Compactor（策略参数按消费方模型窗口定）：

```python
from fusion_harness import Compactor, CompactionConfig

compactor = Compactor(CompactionConfig())
```

2. 每轮循环先判定（`should_compact(messages)` 返回级别字符串，如 `none`）：

```python
if compactor.should_compact(messages) != "none":
    messages = compactor.compact(messages, summarize=my_summarizer)
```

3. 压缩内部按级别降级：先 `reactive_strip`（剥历史），不够再走摘要（`summarize` 回调由消费方注入，摘要质量由回调决定）。

## 验证

```python
est = compactor.estimate_tokens(messages)
print(est)  # 压缩前后各打一次，确认下降
```

## 常见坑

- 不给摘要回调就指望高质量摘要：`summarize_messages` 的质量取决于注入的回调，内核只做调度与分级。
- 只在报错后压缩：应在 `should_compact` 为真时主动压缩，等 LLM 端报上下文超限说明预算判定没用上。
