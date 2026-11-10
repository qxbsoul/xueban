# AgentGraph Legacy Tests

本目录中的测试覆盖的是历史 AgentGraph API 与实现面，不构成当前 Phase 1 runtime contract 的默认验收基线。

典型覆盖对象包括：

- `AgentGraph` 图对象
- `SQLiteMemory` / `RedisMemory`
- `SwarmRouter`
- topology 家族
- 旧工作流执行模型

## 默认行为

默认 `pytest -q` 不会收集本目录。

## 如何运行

```bash
pytest -q --run-legacy
```

## 为什么保留

这些测试仍有历史回归价值，但不能继续污染当前 contract-first 主线的默认验证口径。
