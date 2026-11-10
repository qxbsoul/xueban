# server/src/prompts/

S7 三相位 multi-turn prompt 拆分。Phase 2 (2026-05-22) 起：daemon-led DAG +
artifact handoff 取代 LLM tool_use 多轮编排，LLM 只 emit 状态/思考帧。

## 文件

- `phase-1-analyze.ts` — Team-first phase 1：阅读 `<skill>` 块 → 决定子集 → emit "分析目标需求" + "挑选 Team 蓝图" steps
- `phase-1-analyze-agent-first.ts` — Agent-first 变体：goal-driven 推 agent，跳「挑选 Team 蓝图」
- `phase-2-agent.ts` — per-agent 5 substep (identity/persona/model/tools/memory)，daemon 已 emit 蓝图帧，LLM 只 emit substep 状态
- `phase-3-team.ts` — 设置工具集 + Policy 协作规则；DAG 由 daemon 从 team.yaml 预建，phase 3 只总结
- `index.ts` — `composeMultiTurnPrompt(flow: 'team-first' | 'agent-first')` 拼装；含 `ASSEMBLER_HEADER`（身份 + 引用纪律）

## 触发方式

`server/src/assembler.ts` 在 team-backed skill 路径下调 `composeMultiTurnPrompt`，把结果作为 system prompt 喂给 `workflow.scheduler.runDag()`。

`routes/run-sessions.ts` 根据 `teamSpec` 是否存在自动选 flow：
- 有 teamSpec（BMAD/paper-review 等）→ team-first
- 无 teamSpec（裸 agent-team-blueprint）→ agent-first

## 5-step v3 命名（重要）

LLM emit 的 `<sf:step name="...">` 必须是这 5 个之一：

1. 分析目标需求
2. 挑选 Team 蓝图 (team-first only) / 跳过 (agent-first)
3. 配置 Agent 角色
4. 设置工具集
5. Policy 协作规则

这些 step name 属于对外 SSE 契约。旧前端已移除；改名仍是 breaking change，必须同步通知新前端并更新契约与相关测试。

## 跨边界规则

- 纯字符串 / 模板。**不导入** runtime / tool / SDK
- 修改 prompt 前先看 D2/D3/D7 相关 design doc 决议
- ALWAYS / NEVER 段是硬约束，对应 design doc §4.3 「引用 vs 创造」
- **不再使用 LLM tool_use 编排**：team blueprint 来自 daemon 预建（team.yaml），不是 LLM 通过 `register_agent`/`register_edge` 工具构建
