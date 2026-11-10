/** 公司隔离的模型 Token 流水与官方刊例价估算。 */
import { randomUUID } from 'crypto';
import { getDb } from './sqlite';
import { getSetting } from './settings';

export const PRICE_TABLE_KEY = 'llm.priceTable';
export const OFFICIAL_PRICE_CNY: Readonly<Record<string, { input: number; cached_input: number; output: number }>> =
  Object.freeze({
    'mimo-v2.6-pro': { input: 3, cached_input: 0.025, output: 6 },
  });

/** 可用环境变量 XUEBAN_MODEL_PRICES_CNY 覆盖或新增价目（JSON 对象）。 */
export function cnyPriceFor(model: string): { input: number; cached_input: number; output: number } | null {
  let envTable: Record<string, unknown> = {};
  try {
    envTable = JSON.parse(process.env.XUEBAN_MODEL_PRICES_CNY || '{}') as Record<string, unknown>;
  } catch {
    envTable = {};
  }
  const raw = (envTable[model] ?? OFFICIAL_PRICE_CNY[model]) as
    | { input?: unknown; cached_input?: unknown; output?: unknown }
    | undefined;
  if (!raw || typeof raw.input !== 'number' || typeof raw.output !== 'number') return null;
  return {
    input: raw.input,
    cached_input: typeof raw.cached_input === 'number' ? raw.cached_input : 0,
    output: raw.output,
  };
}

/** 兼容旧引用。 */
export const MIMO_OFFICIAL_PRICE_CNY = OFFICIAL_PRICE_CNY['mimo-v2.6-pro'];

export function readPriceTable(): Record<string, ModelPrice> | undefined {
  const raw = getSetting(PRICE_TABLE_KEY);
  if (!raw || typeof raw !== 'object') return undefined;
  const out: Record<string, ModelPrice> = {};
  for (const [model, p] of Object.entries(raw as Record<string, unknown>)) {
    if (p && typeof p === 'object' && typeof (p as ModelPrice).input === 'number' && typeof (p as ModelPrice).output === 'number') {
      out[model] = { input: (p as ModelPrice).input, output: (p as ModelPrice).output };
    }
  }
  return Object.keys(out).length ? out : undefined;
}

export interface TokenUsageRecord {
  id: string;
  ts: number;
  company_id: string;
  session_id: string;
  model: string;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_est_usd: number;
  cost_est_cny: number;
  pricing_status: 'official' | 'unavailable';
}

export interface ModelPrice { input: number; output: number }
export interface UsageToRecord {
  company_id?: string;
  session_id?: string;
  model?: string;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  ts?: number;
}

export function recordUsage(usage: UsageToRecord, priceTable?: Record<string, ModelPrice>): void {
  try {
    const input = Math.max(0, Math.round(Number(usage.input_tokens) || 0));
    const cached = Math.min(input, Math.max(0, Math.round(Number(usage.cached_input_tokens) || 0)));
    const output = Math.max(0, Math.round(Number(usage.output_tokens) || 0));
    const model = usage.model || '';
    const legacyPrice = (priceTable ?? readPriceTable())?.[model];
    const costUsd = legacyPrice && (input || output) ? (input * legacyPrice.input + output * legacyPrice.output) / 1_000_000 : 0;
    const cnyPrice = cnyPriceFor(model);
    const costCny = cnyPrice
      ? ((input - cached) * cnyPrice.input + cached * cnyPrice.cached_input + output * cnyPrice.output) / 1_000_000
      : 0;
    getDb().prepare(`INSERT INTO token_usage
      (id,ts,company_id,session_id,model,input_tokens,cached_input_tokens,output_tokens,cost_est_usd,cost_est_cny,pricing_status)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
      randomUUID(), usage.ts ?? Date.now(), usage.company_id || '', usage.session_id || '', model,
      input, cached, output, costUsd, costCny, cnyPrice ? 'official' : 'unavailable',
    );
  } catch { /* 统计失败不阻断真实模型主链路。 */ }
}

interface UsageSum {
  calls: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_est_usd: number;
  cost_est_cny: number;
}

function companyWhere(companyId?: string): { sql: string; params: string[] } {
  return companyId ? { sql: ' AND company_id = ?', params: [companyId] } : { sql: '', params: [] };
}

export function sumUsageBetween(fromTs: number, toTs: number, companyId?: string): UsageSum {
  const where = companyWhere(companyId);
  return getDb().prepare(`SELECT COUNT(*) AS calls,
    COALESCE(SUM(input_tokens),0) AS input_tokens,
    COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
    COALESCE(SUM(output_tokens),0) AS output_tokens,
    COALESCE(SUM(cost_est_usd),0) AS cost_est_usd,
    COALESCE(SUM(cost_est_cny),0) AS cost_est_cny
    FROM token_usage WHERE ts >= ? AND ts < ?${where.sql}`).get(fromTs, toTs, ...where.params) as UsageSum;
}

export function usageDailyBetween(fromTs: number, toTs: number, companyId?: string): Array<UsageSum & { day: number }> {
  const where = companyWhere(companyId);
  const rows = getDb().prepare(`SELECT ts,input_tokens,cached_input_tokens,output_tokens,cost_est_usd,cost_est_cny
    FROM token_usage WHERE ts >= ? AND ts < ?${where.sql} ORDER BY ts`).all(fromTs, toTs, ...where.params) as Array<Pick<TokenUsageRecord, 'ts' | 'input_tokens' | 'cached_input_tokens' | 'output_tokens' | 'cost_est_usd' | 'cost_est_cny'>>;
  const byDay = new Map<number, UsageSum>();
  for (const row of rows) {
    const day = startOfDay(row.ts);
    const bucket = byDay.get(day) ?? { calls: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, cost_est_usd: 0, cost_est_cny: 0 };
    bucket.calls += 1;
    bucket.input_tokens += row.input_tokens;
    bucket.cached_input_tokens += row.cached_input_tokens;
    bucket.output_tokens += row.output_tokens;
    bucket.cost_est_usd += row.cost_est_usd;
    bucket.cost_est_cny += row.cost_est_cny;
    byDay.set(day, bucket);
  }
  return [...byDay.entries()].sort((a, b) => a[0] - b[0]).map(([day, value]) => ({ day, ...value }));
}

export function usageBreakdown(fromTs: number, toTs: number, by: 'model' | 'session_id', limit = 10, companyId?: string): Array<UsageSum & { key: string }> {
  const col = by === 'session_id' ? 'session_id' : 'model';
  const where = companyWhere(companyId);
  return getDb().prepare(`SELECT COALESCE(${col},'') AS key,COUNT(*) AS calls,
    COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
    COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(cost_est_usd),0) AS cost_est_usd,
    COALESCE(SUM(cost_est_cny),0) AS cost_est_cny
    FROM token_usage WHERE ts >= ? AND ts < ?${where.sql} GROUP BY COALESCE(${col},'')
    ORDER BY input_tokens + output_tokens DESC LIMIT ?`).all(fromTs, toTs, ...where.params, limit) as Array<UsageSum & { key: string }>;
}

export function startOfDay(ts: number): number {
  const date = new Date(ts);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}
