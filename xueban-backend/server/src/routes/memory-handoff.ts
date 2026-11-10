/**
 * memory-handoff.ts — 功能线③ 记忆与交接路由（2026-11-05）。
 *
 * 挂载点：/api/memory/handoff（server/src/index.ts 一行 app.use，必须排在
 * proxyFallback 之前）。这是外接模块，主入口不改任何既有逻辑。
 *
 * GET    /api/memory/handoff/stats                  → 概览计数
 * GET    /api/memory/handoff/context?agent=B        → B 当前可读上下文（带来源，每次调用落消费留痕）
 * GET    /api/memory/handoff/context/reads          → 记忆消费留痕审计（谁读走了哪些来源）
 * GET    /api/memory/handoff/profiles/conflicts     → 待裁决的画像冲突（人工定夺入口）
 * POST   /api/memory/handoff/profiles/conflicts/resolve → 裁决冲突（accept 采纳 / dismiss 驳回）
 * GET    /api/memory/handoff/profiles/:subjectId    → 当前画像 + append-only 流水
 * POST   /api/memory/handoff/profiles/writeback     → 长期画像写回（置信度合并，force 可裁决）
 * POST   /api/memory/handoff/profiles/ingest        → 客户反馈规则式自动提炼（兼容老入口，走同一队列）
 * GET    /api/memory/handoff/feedback               → 反馈自动写回队列（统计 + 审计记录）
 * POST   /api/memory/handoff/feedback              → 业务系统投递反馈，自动提炼写回（单条或 items[] 批量）
 * POST   /api/memory/handoff/feedback/run          → 补跑 pending / failed
 * GET    /api/memory/handoff                       → 交接包列表
 * POST   /api/memory/handoff                       → A 下班建交接包（自动汇总快照）
 * GET    /api/memory/handoff/:id                   → 交接包详情（含快照）
 * POST   /api/memory/handoff/:id/accept            → B 接班并注入上下文（幂等）
 *
 * IMPORTANT: 所有字面量路径都注册在 /:id 之前，避免 Express 把 "stats" 当 id。
 */

import { Router, Request, Response } from 'express';
import {
  MemoryHandoffError,
  acceptHandoff,
  contextFor,
  contextReads,
  createHandoff,
  feedbackQueue,
  getHandoff,
  handoffStats,
  ingestFeedback,
  listConflicts,
  listHandoffs,
  profile,
  resolveConflict,
  runFeedbackQueue,
  submitFeedback,
  writeback,
} from '../storage/memory-handoff';
import { apiGuard } from '../middleware/api-guard';

const router = Router();

// 写接口（建交接包 / 接班 / 画像写回 / 反馈提炼）默认只允许本机；
// 远程调用需 SF_API_TOKEN。
router.use(apiGuard());

function fail(res: Response, err: unknown): void {
  if (err instanceof MemoryHandoffError) {
    res.status(err.status).json({ error: { code: 'HANDOFF_ERROR', message: err.message } });
    return;
  }
  res.status(500).json({ error: { code: 'INTERNAL', message: (err as Error).message } });
}

function str(v: unknown, max = 600): string {
  return typeof v === 'string' ? v.slice(0, max) : '';
}

// ── Stats ─────────────────────────────────────────────────────────────────────

router.get('/stats', (_req: Request, res: Response) => {
  res.json(handoffStats());
});

// ── B 当前可读上下文 ─────────────────────────────────────────────────────────

router.get('/context', (req: Request, res: Response) => {
  const agent = str(req.query.agent, 64).trim();
  if (!agent) {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'agent is required' } });
    return;
  }
  const limit = Number(req.query.limit);
  res.json(
    contextFor(
      agent,
      str(req.query.scope_key, 64),
      str(req.query.task_type, 64),
      Number.isFinite(limit) && limit > 0 ? limit : 20,
    ),
  );
});

// ── 记忆消费留痕审计 ─────────────────────────────────────────────────────────
// 纯只读：谁在什么时候读走了哪些来源的记忆（可按 from/to 窗口筛，脏参数回落默认值）。
router.get('/context/reads', (req: Request, res: Response) => {
  const num = (v: unknown): number | undefined => {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  };
  res.json(
    contextReads({
      limit: num(req.query.limit),
      from: num(req.query.from),
      to: num(req.query.to),
    }),
  );
});

// ── 长期画像 ─────────────────────────────────────────────────────────────────

// 必须排在 /profiles/:subjectId 之前，否则 Express 会把 "conflicts" 当成 subjectId。
router.get('/profiles/conflicts', (req: Request, res: Response) => {
  const limit = Number(req.query.limit);
  res.json(
    listConflicts({
      limit: Number.isFinite(limit) && limit > 0 ? limit : 50,
      subject_id: str(req.query.subject_id, 64) || undefined,
    }),
  );
});

// 裁决出口：前端只传「哪条 + 采纳还是驳回」，提议值/置信度/证据由后端从冲突事件里取，
// 不让调用方自己拼 force 写回（拼错就会把「驳回」写成覆盖）。重复裁决返回 404。
router.post('/profiles/conflicts/resolve', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  try {
    res.json(
      resolveConflict({
        subject_type: str(body.subject_type, 32) || 'customer',
        subject_id: str(body.subject_id, 64),
        key: str(body.key, 64),
        decision: str(body.decision, 16),
        actor: str(body.actor, 64) || 'system',
      }),
    );
  } catch (err) {
    fail(res, err);
  }
});

router.get('/profiles/:subjectId', (req: Request, res: Response) => {
  res.json(profile(req.params.subjectId, str(req.query.subject_type, 32) || 'customer'));
});

router.post('/profiles/writeback', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  try {
    res.json(
      writeback({
        subject_type: str(body.subject_type, 32) || 'customer',
        subject_id: str(body.subject_id, 64),
        key: str(body.key, 64),
        value: str(body.value, 500),
        confidence: typeof body.confidence === 'number' ? body.confidence : undefined,
        source: str(body.source, 64) || 'manual',
        evidence: str(body.evidence, 600),
        actor: str(body.actor, 64) || 'system',
        force: body.force === true,
      }),
    );
  } catch (err) {
    fail(res, err);
  }
});

router.post('/profiles/ingest', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  try {
    res.json(
      ingestFeedback({
        subject_id: str(body.subject_id, 64),
        text: str(body.text, 1000),
        subject_type: str(body.subject_type, 32) || 'customer',
        actor: str(body.actor, 64) || 'system',
      }),
    );
  } catch (err) {
    fail(res, err);
  }
});

// ── 反馈自动写回队列（2026-11-06） ────────────────────────────────────────────

// 审计出口：状态筛选 + 最近 N 条。字面量路径必须排在 /:id 之前。
router.get('/feedback', (req: Request, res: Response) => {
  const limit = Number(req.query.limit);
  res.json(
    feedbackQueue({
      status: str(req.query.status, 16) || undefined,
      limit: Number.isFinite(limit) && limit > 0 ? limit : 20,
    }),
  );
});

/**
 * 业务系统接入点：反馈一投进来就自动提炼写回，不需要人点按钮。
 * body 既可单条 {subject_id,text}，也可 {items:[...]} 批量（≤50）。
 * 逐条独立处理 —— 一条脏数据只让那一条 failed，不拖垮整批、也不返回 5xx。
 */
router.post('/feedback', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  const rawItems: unknown[] = Array.isArray(body.items) ? body.items : [body];
  if (rawItems.length === 0 || rawItems.length > 50) {
    res.status(400).json({
      error: { code: 'INVALID_BODY', message: 'items 需为 1~50 条' },
    });
    return;
  }
  const summary = {
    received: rawItems.length,
    deduped: 0,
    processed: 0,
    skipped: 0,
    failed: 0,
    conflicts: 0,
  };
  const items: Array<Record<string, unknown>> = [];
  for (const raw of rawItems) {
    const item = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    const subjectId = str(item.subject_id, 64);
    try {
      const out = submitFeedback({
        subject_id: subjectId,
        text: str(item.text, 1000),
        subject_type: str(item.subject_type, 32) || 'customer',
        source: str(item.source, 64) || 'api',
        source_ref: str(item.source_ref, 120),
        actor: str(item.actor, 64) || 'system',
      });
      items.push({ subject_id: subjectId, ...out });
      if (out.dedup) summary.deduped++;
      if (out.status === 'processed') summary.processed++;
      else if (out.status === 'skipped') summary.skipped++;
      else if (out.status === 'failed') summary.failed++;
      summary.conflicts += out.conflicts;
    } catch (err) {
      items.push({ subject_id: subjectId, status: 'failed', error: (err as Error).message });
      summary.failed++;
    }
  }
  res.status(201).json({ ...summary, items });
});

/** 补跑：服务重启残留的 pending，或之前写回失败的 failed。 */
router.post('/feedback/run', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  const limit = Number(body.limit);
  res.json(
    runFeedbackQueue({
      limit: Number.isFinite(limit) && limit > 0 ? limit : 100,
      include_failed: body.include_failed !== false,
    }),
  );
});

// ── 交接包 ───────────────────────────────────────────────────────────────────

router.get('/', (req: Request, res: Response) => {
  const limit = Number(req.query.limit);
  res.json(
    listHandoffs({
      agent: str(req.query.agent, 64) || undefined,
      status: str(req.query.status, 16) || undefined,
      limit: Number.isFinite(limit) && limit > 0 ? limit : 30,
    }),
  );
});

router.post('/', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  try {
    const out = createHandoff({
      shift_id: str(body.shift_id, 64),
      from_agent: str(body.from_agent, 64),
      to_agent: str(body.to_agent, 64),
      scope_key: str(body.scope_key, 64),
      task_type: str(body.task_type, 64),
      summary: str(body.summary, 600),
    });
    res.status(out.idempotent ? 200 : 201).json(out);
  } catch (err) {
    fail(res, err);
  }
});

router.get('/:id', (req: Request, res: Response) => {
  const found = getHandoff(req.params.id);
  if (!found) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Handoff not found' } });
    return;
  }
  res.json(found);
});

router.post('/:id/accept', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  const to_agent = str(body.to_agent, 64).trim();
  if (!to_agent) {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'to_agent is required' } });
    return;
  }
  try {
    res.json(acceptHandoff(req.params.id, to_agent));
  } catch (err) {
    fail(res, err);
  }
});

export default router;
