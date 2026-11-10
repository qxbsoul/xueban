import { getDb } from './sqlite';

/** 10,000 Token 折算 1 积分。 */
export const TOKENS_PER_CREDIT = 10_000;

export type PlanKey = 'basic' | 'studio';

export interface PlanDefinition {
  key: PlanKey;
  label: string;
  price_cents: number;
  credits: number;
  features: string[];
}

/** 套餐定义（页面文案与额度都在这里，升级只改这一处）。 */
export const PLANS: Record<PlanKey, PlanDefinition> = {
  basic: {
    key: 'basic', label: '体验版', price_cents: 0, credits: 500,
    features: ['3 位预设 AI 员工', '500 基础积分', '10,000 Token = 1 积分'],
  },
  studio: {
    key: 'studio', label: '工作室版', price_cents: 6600, credits: 3000,
    features: ['3000 积分 / 月', '多客户项目管理', '客户品牌知识库', '10,000 Token = 1 积分'],
  },
};

export const CUSTOM_PLAN = {
  key: 'custom' as const,
  label: '专业定制版',
  price_cents: 0,
  features: ['专属 Skill 与员工', '知识库迁移', '定制工作流与部署'],
};

const DEFAULT_PLAN: PlanKey = 'basic';

export interface BillingSummary {
  plan: PlanKey;
  plan_label: string;
  price_cents: number;
  credits_total: number;
  credits_used: number;
  credits_remaining: number;
  percent_used: number;
  tokens_used: number;
  tokens_per_credit: number;
  period_start: string;
  period_end: string;
  features: string[];
}

interface BillingRow {
  company_id: string;
  plan: string;
  credits_total: number;
  period_start: number;
  updated_at: number;
}

function startOfMonth(ts: number): number {
  const date = new Date(ts);
  return new Date(date.getFullYear(), date.getMonth(), 1).getTime();
}

function startOfNextMonth(ts: number): number {
  const date = new Date(ts);
  return new Date(date.getFullYear(), date.getMonth() + 1, 1).getTime();
}

function init(): void {
  getDb().exec(`
    CREATE TABLE IF NOT EXISTS company_billing (
      company_id TEXT PRIMARY KEY,
      plan TEXT NOT NULL DEFAULT 'basic',
      credits_total INTEGER NOT NULL DEFAULT 500,
      period_start INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
  `);
}

function normalizePlan(plan: string): PlanKey {
  return plan === 'studio' ? 'studio' : 'basic';
}

/** 读取（必要时创建）计费行；跨月自动重置本周期用量。 */
function ensureRow(companyId: string): BillingRow {
  init();
  const db = getDb();
  const now = Date.now();
  const existing = db.prepare('SELECT * FROM company_billing WHERE company_id = ?').get(companyId) as BillingRow | undefined;
  if (!existing) {
    const row: BillingRow = {
      company_id: companyId,
      plan: DEFAULT_PLAN,
      credits_total: PLANS[DEFAULT_PLAN].credits,
      period_start: startOfMonth(now),
      updated_at: now,
    };
    db.prepare(`INSERT INTO company_billing (company_id, plan, credits_total, period_start, updated_at)
      VALUES (?, ?, ?, ?, ?)`).run(row.company_id, row.plan, row.credits_total, row.period_start, row.updated_at);
    return row;
  }
  // 新的自然月：积分重新发放（只改周期起点，不改套餐）。
  const monthStart = startOfMonth(now);
  if (existing.period_start < monthStart) {
    db.prepare('UPDATE company_billing SET period_start = ?, updated_at = ? WHERE company_id = ?')
      .run(monthStart, now, companyId);
    return { ...existing, period_start: monthStart, updated_at: now };
  }
  return existing;
}

function tokensSince(companyId: string, fromTs: number): number {
  const row = getDb().prepare(`SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
    FROM token_usage WHERE company_id = ? AND ts >= ?`).get(companyId, fromTs) as { tokens: number } | undefined;
  return Math.max(0, Math.round(Number(row?.tokens) || 0));
}

export function billingSummary(companyId: string): BillingSummary {
  const row = ensureRow(companyId);
  const planKey = normalizePlan(row.plan);
  const plan = PLANS[planKey];
  const tokens = tokensSince(companyId, row.period_start);
  const creditsUsed = Math.floor(tokens / TOKENS_PER_CREDIT);
  const total = row.credits_total || plan.credits;
  const remaining = Math.max(0, total - creditsUsed);
  const percent = total > 0 ? Math.min(100, Math.round((creditsUsed / total) * 1000) / 10) : 0;
  return {
    plan: planKey,
    plan_label: plan.label,
    price_cents: plan.price_cents,
    credits_total: total,
    credits_used: creditsUsed,
    credits_remaining: remaining,
    percent_used: percent,
    tokens_used: tokens,
    tokens_per_credit: TOKENS_PER_CREDIT,
    period_start: new Date(row.period_start).toISOString(),
    period_end: new Date(startOfNextMonth(row.period_start)).toISOString(),
    features: plan.features,
  };
}

/** 模拟付款成功后的升级：切换套餐、重置积分周期（剩余积分 = 新套餐额度）。 */
export function upgradePlan(companyId: string, plan: PlanKey): BillingSummary {
  ensureRow(companyId);
  const now = Date.now();
  getDb().prepare(`UPDATE company_billing SET plan = ?, credits_total = ?, period_start = ?, updated_at = ?
    WHERE company_id = ?`).run(plan, PLANS[plan].credits, now, now, companyId);
  return billingSummary(companyId);
}
