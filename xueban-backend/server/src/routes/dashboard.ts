/**
 * dashboard.ts — 数据看板聚合端点（2026-11-05，整合 XUEBAN-OS PR#3 看板数据层）。
 *
 * GET /api/dashboard/overview?days=30   → 总调用/tokens/成本/run 成功率/活跃项目会话
 * GET /api/dashboard/timeseries?days=30&metric=tokens|calls|cost → 按天聚合数组
 * GET /api/dashboard/breakdown?days=30&by=model|session           → 分布 TopN
 * GET /api/dashboard/briefing?days=7    → 每日简报（零 LLM，纯库内现算）
 *
 * 指标来源全部从我们自己的域取材（token_usage / run_index / conversations /
 * projects），营收类指标按评审结论弃掉。聚合 group by 现算，
 * 不预建聚合表。每日简报对齐 XUEBAN-OS briefing.py 的设计口径：落库事实挖、
 * 零 LLM 调用、同日同内容幂等。
 */

import { Router, Request, Response } from 'express';
import { getDb } from '../storage/sqlite';
import { sumUsageBetween, usageDailyBetween, usageBreakdown, startOfDay, cnyPriceFor } from '../storage/token-usage';
import { xuebanChatCompany, XuebanChatError } from '../services/xueban-chat';

const router = Router();
const DAY_MS = 24 * 60 * 60 * 1000;

async function companyId(req: Request): Promise<string> {
  return (await xuebanChatCompany(req.headers.authorization || '')).id;
}

function dashboardError(res: Response, error: unknown): void {
  const err = error instanceof XuebanChatError ? error : new XuebanChatError(500, 'DASHBOARD_ERROR', '看板数据读取失败');
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

function parseDays(raw: unknown, fallback: number): number {
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.round(n), 365);
}

function windowFor(days: number): { from: number; to: number; prevFrom: number; prevTo: number } {
  const to = Date.now();
  const from = to - days * DAY_MS;
  return { from, to, prevFrom: from - days * DAY_MS, prevTo: from };
}

// ── Overview ──────────────────────────────────────────────────────────────────

router.get('/overview', (req: Request, res: Response) => {
  void (async () => {
  const company = await companyId(req);
  const days = parseDays(req.query.days, 30);
  const { from, to, prevFrom, prevTo } = windowFor(days);

  const usage = sumUsageBetween(from, to, company);
  const prevUsage = sumUsageBetween(prevFrom, prevTo, company);

  const runRow = getDb()
    .prepare(
      `SELECT COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0) AS succeeded,
              COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
              COALESCE(SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END), 0) AS canceled
       FROM run_index WHERE created_at >= ? AND created_at < ?`,
    )
    .get(from, to) as { total: number; succeeded: number; failed: number; canceled: number };

  const prevRunRow = getDb()
    .prepare(`SELECT COUNT(*) AS total FROM run_index WHERE created_at >= ? AND created_at < ?`)
    .get(prevFrom, prevTo) as { total: number };

  const activeRow = getDb()
    .prepare(
      `SELECT
         (SELECT COUNT(*) FROM projects WHERE updated_at >= ?) AS projects,
         (SELECT COUNT(*) FROM conversations WHERE updated_at >= ?) AS conversations`,
    )
    .get(from, from) as { projects: number; conversations: number };

  res.json({
    days,
    window: { from, to },
    currency: 'CNY',
    pricing: (() => {
      const price = cnyPriceFor(String(process.env.XUEBAN_PRICING_MODEL || 'deepseek-flash'));
      return price
        ? { source: '内置刊例价（可用 XUEBAN_MODEL_PRICES_CNY 覆盖）', input_per_million: price.input, cached_input_per_million: price.cached_input, output_per_million: price.output }
        : null;
    })(),
    usage: {
      calls: usage.calls,
      input_tokens: usage.input_tokens,
      cached_input_tokens: usage.cached_input_tokens,
      output_tokens: usage.output_tokens,
      total_tokens: usage.input_tokens + usage.output_tokens,
      cost_est_cny: usage.cost_est_cny,
      deltas: {
        calls: pct(usage.calls, prevUsage.calls),
        total_tokens: pct(usage.input_tokens + usage.output_tokens, prevUsage.input_tokens + prevUsage.output_tokens),
        cost_est_cny: pct(usage.cost_est_cny, prevUsage.cost_est_cny),
      },
    },
    runs: { total: runRow.total, succeeded: runRow.succeeded, failed: runRow.failed, canceled: runRow.canceled, success_rate: runRow.total ? round1((runRow.succeeded / runRow.total) * 100) : null, delta_total: pct(runRow.total, prevRunRow.total) },
    active: { projects: activeRow.projects, conversations: activeRow.conversations },
  });
  })().catch(error => dashboardError(res, error));
});

router.get('/timeseries', (req: Request, res: Response) => {
  void (async () => {
  const company = await companyId(req);
  const days = parseDays(req.query.days, 30);
  const metric = typeof req.query.metric === 'string' ? req.query.metric : 'tokens';
  const { from, to } = windowFor(days);
  const daily = usageDailyBetween(from, to, company);
  const series: Array<Record<string, number | string>> = [];
  const firstDay = startOfDay(from);
  const today = startOfDay(to);
  const map = new Map(daily.map(day => [day.day, day]));
  for (let time = firstDay; time <= today; time += DAY_MS) {
    const hit = map.get(time);
    series.push({
      day: time, calls: hit?.calls ?? 0,
      input_tokens: hit?.input_tokens ?? 0, output_tokens: hit?.output_tokens ?? 0,
      tokens: (hit?.input_tokens ?? 0) + (hit?.output_tokens ?? 0),
      cost_est_cny: hit?.cost_est_cny ?? 0,
    });
  }
  res.json({ days, metric, series });
  })().catch(error => dashboardError(res, error));
});

router.get('/breakdown', (req: Request, res: Response) => {
  void (async () => {
  const company = await companyId(req);
  const days = parseDays(req.query.days, 30);
  const by = req.query.by === 'session' ? 'session_id' : 'model';
  const { from, to } = windowFor(days);
  const items = usageBreakdown(from, to, by, 10, company).map(row => ({
    ...row, total_tokens: row.input_tokens + row.output_tokens,
  }));
  res.json({ days, by, items });
  })().catch(error => dashboardError(res, error));
});

// ── helpers ───────────────────────────────────────────────────────────────────

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}
/** 环比百分比：上期 0 期返回 null（无意义），否则 (cur-prev)/prev*100，保留 1 位。 */
function pct(cur: number, prev: number): number | null {
  if (!prev) return null;
  return round1(((cur - prev) / prev) * 100);
}

export default router;
