# server/

Node 22 + TypeScript 网关（端口 8002）。负责前端统一入口、SSE streaming、tool dispatch、skill / agent / team 数据层、BYOK provider 路由，以及未匹配 `/api/*` 到 Python :8000 的反向代理。

## 启动与验证

```bash
cd server
npm ci
npm run dev             # tsx watch，自动重载
npx tsc --noEmit        # 类型检查（无 emit）
npx vitest run --maxWorkers=2
```

完整双后端从仓库根运行 `node scripts/dev.mjs --no-open`。

## 子目录

- `src/index.ts` — 入口、路由挂载表、Python `proxyFallback`
- `src/lib/` — 核心抽象：ConversationRuntime / Tool / Permission / Session / YAML loaders
- `src/routes/` — Express HTTP 路由
- `src/prompts/` — multi-turn prompt 文件
- `src/loaders/` — SKILL.md / design-system 文件加载器
- `src/storage/` — SQLite / JSON 持久化
- `src/llm-providers/` — Anthropic / GLM / OpenAI SDK 适配
- `src/lib/api-clients/` — ApiClient interface 实现
- `src/lib/tools/` — SkillAnchorTool 集

## 跨边界规则

- 仓库已无前端的根 `src/`；这里的 `src/` 均指 `server/src/`。
- 通过 HTTP / SSE 契约与外部前端通信，不导入任何前端源码。
- 新 Node 路由必须挂在 `src/index.ts` 的 `proxyFallback` 之前。
- 数据持久化通过 `.xueban/` 子目录；运行状态不入库。
- 不要在 routes/ 写复杂业务逻辑——分到 lib/、workflow/ 或 storage/。
- 修改 wire / storage 契约后运行 `npm run test:wire`、`npm run test:fixtures`，必要时更新根目录的新前端契约。

## 测试

Vitest、supertest、tsx 与 TypeScript 都在本目录的 devDependencies 中。配置为 `server/vitest.config.ts`，从 `server/` 启动。

旧 script-style 测试使用 `npm run test:scripts` 单独运行，不与 Vitest 结果混报。

## 不要碰

- `.xueban/sessions/` / `.xueban/projects/` — 运行时状态。
- 不要改名 `.xueban/agents/*.agent.yaml` / `.xueban/teams/*.team.yaml`，它们是用户可编辑的源材料。
