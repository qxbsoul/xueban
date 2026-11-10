/**
 * index.ts — Xueban API server
 * Port: 8002
 */

// MUST be the first import: loads repo-root .env into process.env before any
// router module (which may read env at module scope) is evaluated.
import './env';

import express from 'express';
import cors from 'cors';
import path from 'path';
import fs from 'fs';
import runSessionsRouter, { shutdownActiveRuns } from './routes/run-sessions';
import runsRouter from './routes/runs';
import runSuggestionsRouter from './routes/run-suggestions';
import agentsRouter from './routes/agents';
import teamsRouter from './routes/teams';
import skillsRouter from './routes/skills';
import designSystemsRouter from './routes/design-systems';
import exportRouter from './routes/export';
import settingsRouter from './routes/settings';
import cliRouter from './routes/cli';
// ClawHub（2026-06）— 安装会话 REST + SSE
import clawhubRouter from './routes/clawhub';
import acpRouter from './routes/acp';
import { groupsChatRouter } from './routes/groups-chat';
// 后端合一·阶段1b（2026-06-12）— Node 自服务 /api/groups（仅 SF_GROUPS_BACKEND==='node'
// 时挂载，挂在 proxyFallback 之前）。默认（python / 未设）不挂 → 仍转 Python，现状零变化。
import { groupsNodeRouter, handleDmResolve } from './routes/groups-node';
// 2026-05-30 — 后端目录浏览器(给 CLI cwd 选工作目录;浏览器拿不到真实磁盘路径)
import { fsBrowseRouter } from './routes/fs-browse';
// 2026-08-19 — 本地仓库知识库(注册表 + 扫描 + 安全读取,Obsidian vault 模式)
import { knowledgeVaultsRouter } from './routes/knowledge-vaults';
// Story 15.14 — POST /api/artifacts/lint
import artifactsRouter from './routes/artifacts';
// Part D — LLM protocol entrypoints (Anthropic + OpenAI compatible)
import llmRouter from './routes/llm';
import { detectAll } from './cli-detector';
import { detectAcpAgents } from './acp-detector';
import projectsRouter from './routes/projects';
import memoryEntriesRouter from './routes/memory-entries';
// 经验记忆 + 数据看板（2026-11-05，整合 XUEBAN-OS PR#3 设计：memory.py + ledger/看板）
import memoryLessonsRouter from './routes/memory-lessons';
import dashboardRouter from './routes/dashboard';
// 功能线③ 另一半（2026-11-05）：agent 间记忆交接 + 长期画像写回。外接 Router，
// 主入口只挂一行，不新增服务/端口，也不改既有路由语义。
import memoryHandoffRouter from './routes/memory-handoff';
import conversationsRouter, {
  projectScopedConversationsRouter,
} from './routes/conversations';
import authRouter from './routes/auth';
import xuebanChatRouter from './routes/xueban-chat';
import xuebanKnowledgeRouter from './routes/xueban-knowledge';
import xuebanTaskExecutionRouter from './routes/xueban-task-execution';
import xuebanOrgRouter from './routes/xueban-org';
import scheduleRunRouter from './routes/schedule-run';
import xuebanBillingRouter from './routes/xueban-billing';
import { proxyFallback } from './proxy-fallback';
import { initSqlite } from './storage/sqlite';
// B2 中断恢复收尾 — 启动清扫上一进程遗留的孤儿 hop / NULL run_index（2026-11-05 迭代三）
import { sweepOrphanHops } from './storage/agent-hops';
import { sweepOrphanRunIndex } from './storage/run-index';
import { reloadSkills } from './skills';
import {
  reloadDesignSystems,
  seedBuiltinDesignSystems,
} from './design-systems';

const PORT = Number(process.env.PORT ?? 8002);
const FRONTEND_ORIGIN = process.env.FRONTEND_ORIGIN ?? 'http://localhost:3007';

const app = express();

// ── Middleware ────────────────────────────────────────────────────────────────

app.use(cors({
  origin: [FRONTEND_ORIGIN, 'http://localhost:3000', 'http://localhost:5173'],
  credentials: true,
}));

app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true, limit: '1mb' }));

// Request logger (minimal)
app.use((req, _res, next) => {
  console.log(`[${new Date().toISOString()}] ${req.method} ${req.path}`);
  next();
});

// ── Routes ────────────────────────────────────────────────────────────────────

app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    server: 'xueban-api',
    version: '1.0.0',
    port: PORT,
    anthropic_key_configured: !!process.env.ANTHROPIC_API_KEY,
    timestamp: new Date().toISOString(),
  });
});

// Story 15.16 — sqlite must be initialized before any storage-backed route
// runs. initSqlite() is idempotent (cached singleton) so calling it here +
// later from inside route handlers via getDb() is fine.
initSqlite();

// B2 中断恢复收尾（2026-11-05 迭代三）— 单进程启动时刻不存在活 DAG 回合，
// sqlite 里所有未结算 hop / NULL 状态 run_index 都是上一进程崩溃残留，补结算
// （与 run-event-bus 启动 hydration 的「中断 run 补 canceled 帧」配套，审计链
// 两侧都不悬空）。失败仅告警，不阻塞启动。
try {
  const sweptHops = sweepOrphanHops();
  const sweptRuns = sweepOrphanRunIndex();
  if (sweptHops > 0 || sweptRuns > 0) {
    console.log(`[boot] crash-recovery sweep: ${sweptHops} orphan hop(s), ${sweptRuns} stale run_index row(s)`);
  }
} catch (e) {
  console.warn('[boot] crash-recovery sweep failed (non-fatal):', e);
}
initSqlite();

app.use('/api/run-sessions', runSessionsRouter);
app.use('/api/runs', runsRouter);
// 2026-08-23 — 完成态「后续建议」：GET /api/runs/:runId/suggestions。
// 独立 router 同挂 /api/runs（路径不与 runs.ts 冲突），必须在 proxyFallback 之前。
app.use('/api/runs', runSuggestionsRouter);
app.use('/api/agents', agentsRouter);
// S0.7 — Team DAG endpoints (yaml-backed). Mounted before proxyFallback so
// /:id/dag and /:id/dag/validate hit Node; non-matching /api/teams paths
// (root list, create, etc.) fall through to Python via proxyFallback.
app.use('/api/teams', teamsRouter);
app.use('/api/skills', skillsRouter);
app.use('/api/design-systems', designSystemsRouter);
app.use('/api/export', exportRouter);
// Story 15.9 — generation overrides discovery (env-locked model surface).
app.use('/api/settings', settingsRouter);
// Story 15.19 v2 — Local CLI auto-discovery + spawn bridge.
app.use('/api/cli', cliRouter);
// ClawHub（2026-06）— Claw 安装会话（POST 建会话 + GET SSE 日志流 + cancel）。
app.use('/api/clawhub', clawhubRouter);
// Story 15.23 — ACP / MCP remote agent discovery.
app.use('/api/acp', acpRouter);
// Story 15.14 — POST /api/artifacts/lint
app.use('/api/artifacts', artifactsRouter);
// SIWE + guest authentication
app.use('/api/auth', authRouter);
// Story 15.16 — Project + Conversation persistence layer.
app.use('/api/projects', projectsRouter);
app.use('/api/projects', projectScopedConversationsRouter);
// Story 16.1 — River Memory CRUD
app.use('/api/memory-entries', memoryEntriesRouter);
// 经验记忆（自动沉淀 + 分域召回）与数据看板聚合。必须挂在 proxyFallback 之前。
app.use('/api/memory/lessons', memoryLessonsRouter);
app.use('/api/memory/handoff', memoryHandoffRouter);
app.use('/api/dashboard', dashboardRouter);
app.use('/api/conversations', conversationsRouter);
// Chat/DM/群聊 发消息网关 — 拦下 POST /api/groups/:id/messages 用 Node dispatcher
// 生成回复（CLI + API 都真生效）。其余 /api/groups/* 不在此定义 → next() →
// proxyFallback 转 Python。必须挂在 proxyFallback 之前。
// 2026-05-30 修：此前只 import 未 app.use，发消息漏过网关落到 Python chat-bridge，
// 导致 CLI executor 永远走不通（Python spawn 不了 CLI）。
app.use('/api/groups', groupsChatRouter);
// 1b — Node 自服务 /api/groups（建群/list/get/patch/messages 读+兜底持久化/reactions/
// pin/user-settings/SSE）。挂在 groupsChatRouter **之后**（POST /:id/messages +
// GET /:id/timeline 仍先被网关拦截跑回复/聚合），proxyFallback **之前**。仅
// SF_GROUPS_BACKEND==='node' 时挂载；默认（python/未设）不挂 → /api/groups/* 仍
// 经 proxyFallback 转 Python，行为零变化。DM resolve 在 /api/chat/* 下单独挂。
if (process.env.SF_GROUPS_BACKEND === 'node') {
  app.use('/api/groups', groupsNodeRouter);
  app.post('/api/chat/dm/:agent_id/resolve', handleDmResolve);
  console.log('[groups] SF_GROUPS_BACKEND=node — /api/groups served by Node SQLite store');
}
// 后端目录浏览器(GET /api/fs/list|home|validate)。Node 读本机磁盘列目录,前端逐层
// 点选 → 回传真实绝对路径当 CLI cwd。挂在 proxyFallback 之前,否则会被转去 Python。
app.use('/api/fs', fsBrowseRouter);
// 2026-08-19 — 本地仓库知识库(Obsidian vault 模式,注册表移植自 Shadow):
// 注册/浏览/阅读本地文件夹,只读用户文件。同样必须挂在 proxyFallback 之前。
app.use('/api/knowledge-vaults', knowledgeVaultsRouter);
// Part D — LLM protocol entrypoints. MUST be mounted BEFORE proxyFallback
// otherwise /api/llm/* would be forwarded to Python instead of handled here.
app.use('/api/llm', llmRouter);
// 公司账号隔离的真实模型单聊/群聊；会话与消息存 Node SQLite，公司归属由 Python 校验。
app.use('/api/xueban-chat', xuebanChatRouter);
app.use('/api/xueban-knowledge', xuebanKnowledgeRouter);
// Python 项目执行专用真实模型补全；业务状态仍只由 Python 写入。
app.use('/api/xueban-internal', xuebanTaskExecutionRouter);
// AI 组织草案仅走真实 MiMo；确认后才由 Python 单事务发布。
app.use('/api/xueban-org', xuebanOrgRouter);
// 定时任务「立即执行」：优先交给 Python；旧实例没有执行桥时由 Node 兜底执行并回写运行记录。
app.use('/api/schedules', scheduleRunRouter);
// 套餐与积分：按月发放、10,000 Token = 1 积分；升级为模拟付款。
app.use('/api/xueban-billing', xuebanBillingRouter);
// 多模态知识包只能经公司归属路由访问；Node 内部仍直接调用 Python。
// 阻断浏览器绕过 /api/xueban-knowledge 直接列举或读取其他公司的资料。
app.use('/api/memory-bank', (_req, res) => {
  res.status(404).json({ error: { code: 'KNOWLEDGE_ROUTE_PRIVATE', message: '请通过公司知识库接口访问' } });
});

// /api/groups — formerly a placeholder stub that returned synthetic IDs and
// empty arrays. Removed 2026-05-19 so groups requests fall through to the
// Python FastAPI backend (xueban/api/groups.py), which persists groups
// + messages under .xueban/groups/*.json. The stub was masking real
// data: `createGroup` succeeded with a fake id, but the group never existed
// in any storage so `/chat` always rendered "暂无群组". See Step 4 of the
// data-vertical plan.

// ── Fallback to Python FastAPI (default :8000) ────────────────────────────────
// Single-port UX: any /api/* not matched by the 12 Node routers above is
// transparently proxied to the Python backend. Mounted BEFORE the 404 catch-all
// so unmatched /api/* paths reach Python instead of returning a Node 404.
app.use('/api', proxyFallback);

// ── Static artifacts (Story 15.2) ─────────────────────────────────────────────
// Serves files written by runSkillAssembler under .xueban/projects/<id>/
// e.g. /projects/<session_id>/prototype.html for the 15.3 iframe preview.
const projectsRoot = path.join(process.cwd(), '.xueban', 'projects');
try {
  fs.mkdirSync(projectsRoot, { recursive: true });
} catch (err) {
  console.warn(`[index] could not create projects dir ${projectsRoot}:`, (err as Error).message);
}
// SECURITY (review B1, OpenDesign architecture.md): even though the iframe sets
// sandbox="allow-scripts" client-side, we add a CSP `sandbox` response header as
// defense-in-depth so direct nav (新标签打开 / 截图工具) also gets the same
// isolation. `sandbox` with no token = strictest opaque origin — scripts run but
// cannot reach top.localStorage even if the user opens the artifact directly.
app.use('/projects', express.static(projectsRoot, {
  maxAge: 0,
  etag: false,
  setHeaders: (res) => {
    res.setHeader('Content-Security-Policy', "sandbox allow-scripts; default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:");
    res.setHeader('X-Content-Type-Options', 'nosniff');
  },
}));
console.log(`[index] static /projects → ${projectsRoot}`);

// 404 catch-all
app.use((_req, res) => {
  res.status(404).json({ error: 'Not found' });
});

// ── Story 15.10: hot-load FS skills at boot ───────────────────────────────────
// Scans .xueban/skills/<id>/SKILL.md and merges into the in-memory
// registry. Errors are non-fatal (logged + skipped per file).
{
  const { reloaded, errors } = reloadSkills();
  if (reloaded > 0 || errors.length > 0) {
    console.log(`[index] skill-loader: ${reloaded} loaded, ${errors.length} error(s)`);
  }
}

// ── Story 15.11: seed + hot-load FS Design Systems at boot ────────────────────
// 1. Seed `.xueban/design-systems/{tailwind,material,shadcn,none}.md` if
//    they don't already exist (idempotent — never overwrites user edits).
// 2. Scan the dir and merge any FS DS into the registry (FS overrides
//    same-id built-ins). Errors are non-fatal.
{
  const seed = seedBuiltinDesignSystems();
  if (seed.written.length > 0) {
    console.log(`[index] design-system-loader: seeded ${seed.written.join(', ')}`);
  }
  const { reloaded, failed, overrides } = reloadDesignSystems();
  if (reloaded > 0 || failed > 0) {
    console.log(
      `[index] design-system-loader: ${reloaded} loaded, ${failed} error(s)` +
        (overrides.length > 0 ? `, override: ${overrides.join(', ')}` : ''),
    );
  }
}

// ── Story 15.19 v2: warm CLI detection cache (fire-and-forget) ────────────────
// First scan happens in the background so server boot isn't blocked. The cache
// is populated by the time anyone calls GET /api/cli/detect (and that endpoint
// itself awaits the same promise via detectAll()).
detectAll(true)
  .then((snap) => {
    const installed = snap.items.filter((i) => i.installed);
    console.log(
      `[index] cli-detector: ${installed.length}/${snap.items.length} CLI(s) detected` +
        (installed.length > 0 ? ` — ${installed.map((i) => i.id).join(', ')}` : ''),
    );
  })
  .catch((err) => {
    console.warn(`[index] cli-detector boot scan failed: ${(err as Error).message}`);
  });

// ── Story 15.23: warm ACP / MCP detection cache (fire-and-forget) ──────────────
// Non-blocking PATH + TCP-ping scan. installed:false is normal on a clean dev
// machine and never aborts boot.
detectAcpAgents(true)
  .then((snap) => {
    const online = snap.items.filter((i) => i.installed);
    console.log(
      `[index] acp-detector: ${online.length}/${snap.items.length} agent(s) installed` +
        (online.length > 0 ? ` — ${online.map((i) => `${i.id}(${i.transport})`).join(', ')}` : ''),
    );
  })
  .catch((err) => {
    console.warn(`[index] acp-detector boot scan failed: ${(err as Error).message}`);
  });

// ── Start ─────────────────────────────────────────────────────────────────────

// ── Graceful shutdown (T3 A8) ───────────────────────────────────────────────
// On SIGTERM/SIGINT, cancel all active run-sessions so their spawned CLI/ACP
// child processes get SIGTERM→SIGKILL'd instead of orphaning + burning tokens.
let shuttingDown = false;
async function gracefulShutdown(sig: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[index] ${sig} received — cancelling active runs…`);
  try {
    await shutdownActiveRuns(3000);
  } catch (e) {
    console.warn(`[index] shutdownActiveRuns failed: ${(e as Error).message}`);
  } finally {
    process.exit(0);
  }
}
process.on('SIGTERM', () => void gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => void gracefulShutdown('SIGINT'));

// ── Start ───────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  const mode = process.env.ANTHROPIC_API_KEY ? 'Claude-powered (server key)' : 'BYOK (user-supplied key)';
  console.log(`
╔══════════════════════════════════════════════════════╗
║          Xueban API Server — Port ${PORT}          ║
╚══════════════════════════════════════════════════════╝
  Mode     : ${mode}
  CORS     : ${FRONTEND_ORIGIN}
  Health   : http://localhost:${PORT}/health
  Sessions : http://localhost:${PORT}/api/run-sessions
`);
});
