/**
 * run-suggestions.test.ts — GET /api/runs/:runId/suggestions 路由集成测试。
 *
 * Run (from repo root):
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/run-suggestions.test.ts
 *
 * LLM 经 createRunSuggestionsRouter({ generate }) 注入 mock（不碰真实 key / 网络）：
 *   1. daemon run（registerGatewayRun 播种 + 时间线帧）+ generate 返回建议
 *      → 200 { suggestions }，且 generate 收到的 ctx 含真实 goal / 产物
 *   2. generate 返回 null / [] / 抛错 → 502 + suggestions: []（严禁假建议）
 *   3. 未知 runId → 404
 *
 * Isolation: 与 runs-projections.test.ts 同模式——run-sessions.ts 模块加载
 * 有副作用（mkdir .xueban/*、磁盘 hydrate），先 chdir 到一次性 tmp 目录
 * 再动态 import，测完恢复 cwd 并清理。
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import express from 'express';
import type { AddressInfo } from 'node:net';
import type http from 'node:http';
import type { RunSuggestionContext } from '../../lib/run-suggestions';

let createRunSuggestionsRouter: typeof import('../run-suggestions').createRunSuggestionsRouter;
let registerGatewayRun: typeof import('../run-sessions').registerGatewayRun;
let server: http.Server;
let base: string;
let origCwd: string;
let tmp: string;

/** 当前用例的 generate mock（beforeEach 重置为 null → 502）。 */
let generateImpl: (ctx: RunSuggestionContext) => Promise<string[] | null>;
const seenCtx: RunSuggestionContext[] = [];

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-run-suggestions-'));
  process.chdir(tmp);
  ({ createRunSuggestionsRouter } = await import('../run-suggestions'));
  ({ registerGatewayRun } = await import('../run-sessions'));

  const app = express();
  app.use(express.json());
  app.use(
    '/api/runs',
    createRunSuggestionsRouter({
      generate: async (ctx) => {
        seenCtx.push(ctx);
        return generateImpl(ctx);
      },
    }),
  );
  await new Promise<void>((resolve) => {
    server = app.listen(0, '127.0.0.1', resolve);
  });
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  process.chdir(origCwd);
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

beforeEach(() => {
  generateImpl = async () => null;
  seenCtx.length = 0;
});

/** 播种一条已完成 daemon run：goal + 产物卡 + 最终回复。 */
function seedFinishedRun(): string {
  const handle = registerGatewayRun({ goal: '写一份氢能政策简报', conversation_id: 'conv-sugg' });
  handle.emit('message', { id: 'u1', kind: 'user_turn', turn_id: 't1', ts: 100, text: '写一份氢能政策简报' });
  handle.emit('message', {
    id: 'a1',
    kind: 'artifact_card',
    turn_id: 't1',
    ts: 110,
    title: '氢能政策简报.md',
    artifact_type: 'markdown',
    filename: '氢能政策简报.md',
  });
  handle.emit('message', { id: 'r1', kind: 'assistant_text', turn_id: 't1', ts: 120, body: '简报已完成，覆盖三条补贴政策。' });
  handle.finish('succeeded');
  return handle.session_id;
}

describe('GET /api/runs/:runId/suggestions', () => {
  it('已完成 run + LLM 正常 → 200，建议透传，ctx 含真实 goal/产物', async () => {
    generateImpl = async () => ['把简报导出为 PDF 发给我', '补充各补贴政策的申报截止日期'];
    const id = seedFinishedRun();
    const res = await fetch(`${base}/api/runs/${id}/suggestions`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { suggestions: string[] };
    expect(body.suggestions).toEqual(['把简报导出为 PDF 发给我', '补充各补贴政策的申报截止日期']);
    expect(seenCtx).toHaveLength(1);
    expect(seenCtx[0]!.goal).toBe('写一份氢能政策简报');
    expect(seenCtx[0]!.artifacts).toContain('氢能政策简报.md');
    expect(seenCtx[0]!.summary).toContain('简报已完成');
  });

  it('LLM 不可用（generate → null）→ 502 + 空数组', async () => {
    generateImpl = async () => null;
    const id = seedFinishedRun();
    const res = await fetch(`${base}/api/runs/${id}/suggestions`);
    expect(res.status).toBe(502);
    const body = (await res.json()) as { error: string; suggestions: string[] };
    expect(body.error).toBe('suggestions_unavailable');
    expect(body.suggestions).toEqual([]);
  });

  it('LLM 解析为空（generate → []）→ 502 + 空数组', async () => {
    generateImpl = async () => [];
    const id = seedFinishedRun();
    const res = await fetch(`${base}/api/runs/${id}/suggestions`);
    expect(res.status).toBe(502);
    const body = (await res.json()) as { suggestions: string[] };
    expect(body.suggestions).toEqual([]);
  });

  it('generate 抛错 → 502 + 空数组', async () => {
    generateImpl = async () => {
      throw new Error('boom');
    };
    const id = seedFinishedRun();
    const res = await fetch(`${base}/api/runs/${id}/suggestions`);
    expect(res.status).toBe(502);
    const body = (await res.json()) as { error: string; suggestions: string[] };
    expect(body.error).toBe('suggestions_unavailable');
    expect(body.suggestions).toEqual([]);
  });

  it('未知 runId → 404（不调 generate）', async () => {
    generateImpl = async () => ['不应被调用'];
    const res = await fetch(`${base}/api/runs/no-such-run/suggestions`);
    expect(res.status).toBe(404);
    expect(seenCtx).toHaveLength(0);
  });
});
