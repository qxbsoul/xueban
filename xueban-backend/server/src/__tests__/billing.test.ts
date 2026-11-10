import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import billingRouter from '../routes/xueban-billing';
import { billingSummary, TOKENS_PER_CREDIT, upgradePlan } from '../storage/billing';
import { recordUsage } from '../storage/token-usage';
import { _resetForTests, getDb } from '../storage/sqlite';

const app = express();
app.use(express.json());
app.use('/api/xueban-billing', billingRouter);

const originalCwd = process.cwd();
let dir = '';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-billing-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    if (String(input).includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
    return Response.json({ items: [] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

const addTokens = (tokens: number, ts = Date.now()) => {
  recordUsage({ company_id: 'co1', session_id: 's1', model: 'mimo-v2.6-pro', input_tokens: tokens, output_tokens: 0, ts });
};

describe('积分与套餐', () => {
  it('新公司默认体验版，500 积分，10,000 Token = 1 积分', () => {
    const summary = billingSummary('co1');
    expect(summary).toMatchObject({ plan: 'basic', plan_label: '体验版', credits_total: 500, credits_used: 0, percent_used: 0 });
    expect(summary.tokens_per_credit).toBe(TOKENS_PER_CREDIT);
    addTokens(25_000);
    const used = billingSummary('co1');
    expect(used.tokens_used).toBe(25_000);
    expect(used.credits_used).toBe(2);
    expect(used.credits_remaining).toBe(498);
    expect(used.percent_used).toBe(0.4);
  });

  it('升级工作室版：积分重置为 3000，套餐名不含敏感措辞', () => {
    addTokens(100_000);
    expect(billingSummary('co1').credits_used).toBe(10);
    const upgraded = upgradePlan('co1', 'studio');
    expect(upgraded).toMatchObject({ plan: 'studio', plan_label: '工作室版', credits_total: 3000, credits_used: 0, percent_used: 0 });
    expect(upgraded.price_cents).toBe(6600);
    expect(JSON.stringify(upgraded)).not.toMatch(/VIP|vip/);
  });

  it('跨月自动重置本周期用量', () => {
    billingSummary('co1'); // 首次读取会建表
    addTokens(50_000, Date.now() - 40 * 24 * 3600 * 1000);
    getDb().prepare('UPDATE company_billing SET period_start = ?').run(Date.now() - 40 * 24 * 3600 * 1000);
    // 触发 ensureRow：旧周期（上个月）应被重置，历史 Token 不再计入。
    const summary = billingSummary('co1');
    expect(summary.credits_used).toBe(0);
    expect(new Date(summary.period_start).getDate()).toBe(1);
  });

  it('HTTP：需要登录，升级只接受工作室版', async () => {
    await request(app).get('/api/xueban-billing/summary').expect(401);
    const ok = await request(app).get('/api/xueban-billing/summary').set('Authorization', 'Bearer co1').expect(200);
    expect(ok.body.data.plan_label).toBe('体验版');
    const upgraded = await request(app).post('/api/xueban-billing/upgrade').set('Authorization', 'Bearer co1').send({ plan: 'studio' }).expect(200);
    expect(upgraded.body.data.credits_total).toBe(3000);
    expect(upgraded.body.meta.simulated_payment).toBe(true);
    const invalid = await request(app).post('/api/xueban-billing/upgrade').set('Authorization', 'Bearer co1').send({ plan: 'custom' }).expect(422);
    expect(invalid.body.error.code).toBe('INVALID_PLAN');
  });
});
