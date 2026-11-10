# server/src/routes/

Express HTTP 路由。每个文件一个 prefix，挂在 `src/index.ts` 里。

## 主要路由

| Prefix | 文件 | 用途 |
|---|---|---|
| `/api/run-sessions` | `run-sessions.ts` | SSE streaming 主入口（POST 建 session，GET /:id/stream 拉流） |
| `/api/runs` | `runs.ts` | 历史 run 列表 + 详情 |
| `/api/agents` | `agents.ts` | sqlite + yaml 合并 agent 列表（S0.6 `listAllAgents` merge） |
| `/api/teams` | `teams.ts` (S0.7) | `:id/dag` GET/PUT/POST validate；其他路径 fallthrough 到 Python |
| `/api/skills` | `skills.ts` | 列 skill + ingest + `:skillId/team` + `:skillId/agents/:id/:slot` (S6.1) |
| `/api/design-systems` | `design-systems.ts` | DS 列表 + frontmatter |
| `/api/export` | `export.ts` | 导出 artifact |
| `/api/settings` | `settings.ts` | 应用设置 / BYOK / model overrides |
| `/api/cli` | `cli.ts` | 本地 CLI 探测（claude / gh-copilot / openclaw） |
| `/api/acp` | `acp.ts` | ACP / MCP 远程 agent 探测 |
| `/api/artifacts` | `artifacts.ts` | artifact lint / 校验 |

## 后挂 fallback

`src/index.ts:116` 末尾 `app.use('/api', proxyFallback)` — 未匹配 Node 路由的 /api/* 转发到 Python (`:8000`)。挂载顺序很关键。

## 跨边界规则

- 路由文件**只**负责 HTTP shape 转换（req.body → service args → res.json）
- 复杂业务到 `src/lib/` 或 `src/storage/`
- SSE writer 用 `res.write('data: ...\n\n')` 模式（参考 run-sessions.ts:sendEvent）
- 不要在 routes 直接读 yaml / 调 LLM SDK（套间接 lib）

## 添加新路由

1. 在 `src/routes/<name>.ts` 写一个 Router
2. `export default router`
3. `src/index.ts` 加 `import + app.use('/api/<name>', xRouter)`，**挂在 proxyFallback 之前**
