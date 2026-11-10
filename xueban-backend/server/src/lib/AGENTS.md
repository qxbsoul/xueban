# server/src/lib/

核心抽象层。**纯逻辑**——不直接动 HTTP / fs / network。每个 lib 是独立单元，依赖关系自上而下：

## 数据类型

- `conversation-types.ts` (S1) — `ContentBlock` / `ConversationMessage` / `TokenUsage`，与 Anthropic Messages API ContentBlock 对齐（内部用 `kind`，wire 用 `type`，通过 `anthropic-block-adapter.ts` 转换）
- `contracts.ts` — SSE event shape / step artifact / output_kind 等老契约（S2.x 前的）
- `skill-types.ts` (S6.0) — `SkillAgentDef` / `TeamDef` / `SkillAnchor` / `SkillSlot`

## Skill / Agent / Team 数据层

- `skill-yaml.ts` (S6.0 legacy) — 老 team.skill.yaml 加载器（per-skill 内嵌 agents）
- `agent-yaml.ts` (S0.5) — 全局 agent 库 `.xueban/agents/*.agent.yaml`
- `team-yaml.ts` (S0.5) — 全局 team 库 `.xueban/teams/*.team.yaml` + DAG 校验

## 运行时核心

- `tool-spec.ts` (S2) — `ToolSpec` + `ToolRegistry`
- `permission-policy.ts` (S3) — allow / deny (D6 决议无 prompt 模式) + `fromAllowedTools` factory
- `conversation-runtime.ts` (S5) — 16→50 turn agentic loop + ApiClient/ToolExecutor trait
- `anthropic-block-adapter.ts` (S5) — `kind` ↔ `type` 双向映射 + S5 P0 fold 连续 tool messages

## 子目录

- `tools/` — SkillAnchorTool 集 + executor 包装（S4）
- `api-clients/` — ApiClient interface 实现 layer（S6）

## 存储层

- `session-store.ts` — Cherry-Studio 模式 JSON 文件持久化（atomic .tmp + rename）+ S1 migrate hook
- `step-store.ts` — step artifact 持久化
- `intent-router.ts` (S1.1) — TS 翻译 Python IntentRouter

## 跨边界规则

- 不导入 `routes/` / `storage/sqlite.ts` / `llm-providers/`
- 不直接读 process.cwd() 之外的路径
- 不抛硬错——返回 `{ output, isError: true }` shape 或 union type
- 每个 lib 自带 `__tests__/<lib>.test.ts` 单测

## 测试

`cd server && npx vitest run src/lib/__tests__/<name>.test.ts`。Vitest 已包含在 `server/` 的 devDependencies 中；旧 script-style 测试可用 `npx tsx <file>` 单独运行。
