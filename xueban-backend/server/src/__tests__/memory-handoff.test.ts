/**
 * memory-handoff.test.ts — 功能线③ 契约测试（2026-11-05）。
 *
 * 真实 Express Router + 真实 SQLite（临时 cwd 隔离），覆盖：
 *   - A 下班自动汇总快照（分域经验 + 未决事项），同班次幂等
 *   - 接班前 B 读不到 A 的上下文；接班后注入副本、来源可溯、重复幂等
 *   - 错误接班人 403 / 不存在的包 404 / 跨域不串专属上下文
 *   - 长期画像写回 create/keep/conflict/update + append-only 流水留痕
 *   - 客户反馈规则式自动提炼（零 LLM）
 *   - 脏输入不 5xx
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let router: typeof import('../routes/memory-handoff').default;
let sqlite: typeof import('../storage/sqlite');
let lessons: typeof import('../storage/memory-lessons');

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/memory/handoff', router);
  return a;
}

let handoffId = '';

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-handoff-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  lessons = await import('../storage/memory-lessons');
  router = (await import('../routes/memory-handoff')).default;
  sqlite.getDb();

  // 三条经验：通用 / 同域 / 跨域（跨域那条绝不能被交接包带走）
  lessons.remember({ content: '交付必须附证据链', scope_key: '', kind: 'lesson', score: 0.9 });
  lessons.remember({
    content: '烘焙文案：标题要带口味词，别写成工作底稿',
    scope_key: 'copywriting', task_type: 'writer', kind: 'lesson', score: 0.8,
  });
  lessons.remember({
    content: '出海选品：只看东南亚站点',
    scope_key: 'overseas', task_type: 'selector', kind: 'lesson', score: 0.8,
  });
  // 一条失败 run，作为「未决事项」的来源
  sqlite.getDb()
    .prepare(
      `INSERT INTO run_index (session_id, conversation_id, goal, status, created_at, updated_at)
       VALUES (?,?,?,?,?,?)`,
    )
    .run('s-failed-1', null, '王老板第二稿还没确认', 'failed', Date.now(), Date.now());
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('agent 间记忆交接', () => {
  it('A 下班：快照自动汇总同域经验与未决事项，不含跨域经验', async () => {
    const res = await request(app()).post('/api/memory/handoff').send({
      shift_id: 'shift-1',
      from_agent: 'A',
      to_agent: 'B',
      scope_key: 'copywriting',
      task_type: 'writer',
      summary: '夜班接手王老板文案单',
    });
    expect(res.status).toBe(201);
    expect(res.body.idempotent).toBe(false);
    const ctx = res.body.handoff.context;
    expect(ctx.lessons.length).toBe(2);
    expect(ctx.lessons.some((l: { content: string }) => l.content.includes('烘焙文案'))).toBe(true);
    expect(ctx.lessons.some((l: { content: string }) => l.content.includes('出海选品'))).toBe(false);
    expect(ctx.open_items.length).toBeGreaterThanOrEqual(1);
    expect(ctx.shift.from_agent).toBe('A');
    handoffId = res.body.handoff.id;
  });

  it('同班次重复提交幂等，列表仍只有 1 份', async () => {
    const res = await request(app()).post('/api/memory/handoff').send({
      shift_id: 'shift-1', from_agent: 'A', to_agent: 'B',
      scope_key: 'copywriting', task_type: 'writer',
    });
    expect(res.status).toBe(200);
    expect(res.body.idempotent).toBe(true);
    const list = await request(app()).get('/api/memory/handoff');
    expect(list.body).toHaveLength(1);
  });

  it('接班前 B 读不到 A 的上下文（通用经验仍可带）', async () => {
    const res = await request(app()).get(
      '/api/memory/handoff/context?agent=B&scope_key=copywriting&task_type=writer',
    );
    expect(res.status).toBe(200);
    expect(res.body.items.some((i: { content: string }) => i.content.includes('烘焙文案'))).toBe(false);
    expect(res.body.items.some((i: { source: string }) => i.source.startsWith('handoff:'))).toBe(false);
    expect(res.body.items.some((i: { source: string }) => i.source === 'lesson:common')).toBe(true);
  });

  it('B 接班：注入副本、来源可溯、重复接班幂等', async () => {
    const res = await request(app())
      .post(`/api/memory/handoff/${handoffId}/accept`)
      .send({ to_agent: 'B' });
    expect(res.status).toBe(200);
    expect(res.body.injected).toBe(2);
    expect(res.body.handoff.status).toBe('accepted');

    const ctx = await request(app()).get(
      '/api/memory/handoff/context?agent=B&scope_key=copywriting&task_type=writer',
    );
    expect(
      ctx.body.items.some(
        (i: { content: string; source: string }) =>
          i.content.includes('烘焙文案') && i.source.startsWith('handoff:'),
      ),
    ).toBe(true);
    expect(ctx.body.sources.some((s: string) => s.startsWith('handoff:'))).toBe(true);

    const again = await request(app())
      .post(`/api/memory/handoff/${handoffId}/accept`)
      .send({ to_agent: 'B' });
    expect(again.body.idempotent).toBe(true);
    expect(again.body.injected).toBe(0);
  });

  it('错误接班人 403 / 不存在的包 404', async () => {
    const wrong = await request(app())
      .post(`/api/memory/handoff/${handoffId}/accept`)
      .send({ to_agent: 'C' });
    expect(wrong.status).toBe(403);
    const missing = await request(app())
      .post('/api/memory/handoff/no-such/accept')
      .send({ to_agent: 'B' });
    expect(missing.status).toBe(404);
  });

  it('跨域不串专属上下文（交接包本身也不出现）', async () => {
    const res = await request(app()).get(
      '/api/memory/handoff/context?agent=B&scope_key=overseas&task_type=selector',
    );
    expect(res.body.items.some((i: { content: string }) => i.content.includes('烘焙文案'))).toBe(false);
    expect(res.body.items.some((i: { type: string }) => i.type === 'handoff')).toBe(false);
  });
});

describe('长期画像写回', () => {
  it('create / keep / conflict / update 四动作 + append-only 流水留痕', async () => {
    const wb = (confidence: number, value: string) =>
      request(app()).post('/api/memory/handoff/profiles/writeback').send({
        subject_id: 'cust-1', key: 'preference', value, confidence,
        source: 'manual', evidence: '自检证据', actor: 'selftest',
      });
    expect((await wb(0.9, '偏好简洁')).body.action).toBe('create');
    const keep = await wb(0.5, '偏好华丽');
    expect(keep.body.action).toBe('keep');
    expect(keep.body.value).toBe('偏好简洁');
    const conflict = await wb(0.9, '偏好华丽');
    expect(conflict.body.action).toBe('conflict');
    expect(conflict.body.value).toBe('偏好简洁');
    expect((await wb(0.95, '偏好极简')).body.action).toBe('update');

    const p = await request(app()).get('/api/memory/handoff/profiles/cust-1');
    expect(p.body.writes).toBe(4);
    expect(p.body.conflicts).toBe(1);
    expect(p.body.profile[0].value).toBe('偏好极简');
    const upd = p.body.history.find((h: { action: string }) => h.action === 'update');
    expect(upd.old_value).toBe('偏好简洁');
    expect(upd.new_value).toBe('偏好极简');
  });

  it('客户反馈规则式自动提炼（零 LLM）', async () => {
    const res = await request(app()).post('/api/memory/handoff/profiles/ingest').send({
      subject_id: 'cust-2',
      text: '客户偏好简洁一点，不要长篇，预算 800 以内',
    });
    expect(res.status).toBe(200);
    expect(res.body.profile_keys).toEqual(expect.arrayContaining(['preference', 'budget']));
    const p = await request(app()).get('/api/memory/handoff/profiles/cust-2');
    expect(p.body.profile.map((x: { key: string }) => x.key)).toEqual(
      expect.arrayContaining(['preference', 'budget']),
    );
  });

  it('stats 计数口径正确', async () => {
    const res = await request(app()).get('/api/memory/handoff/stats');
    expect(res.status).toBe(200);
    expect(res.body.handoffs).toBe(1);
    expect(res.body.accepted).toBe(1);
    expect(res.body.handoff_items).toBe(2);
    expect(res.body.conflicts).toBe(1);
  });
});

describe('画像冲突的裁决出口（force + 待裁决列表）', () => {
  const wb = (body: Record<string, unknown>) =>
    request(app()).post('/api/memory/handoff/profiles/writeback').send(body);

  it('同置信不同说法先记冲突，force 覆盖后从待裁决列表消失', async () => {
    await wb({ subject_id: 'cust-c1', key: 'budget', value: '预算 800', confidence: 0.7, source: 'manual' });
    const conflict = await wb({
      subject_id: 'cust-c1', key: 'budget', value: '预算 3000', confidence: 0.7, source: 'manual',
    });
    expect(conflict.body.action).toBe('conflict');
    expect(conflict.body.value).toBe('预算 800'); // 不覆盖

    const list = await request(app()).get('/api/memory/handoff/profiles/conflicts');
    expect(list.status).toBe(200);
    const row = list.body.find((x: { subject_id: string }) => x.subject_id === 'cust-c1');
    expect(row).toBeTruthy();
    expect(row.current_value).toBe('预算 800');
    expect(row.proposed_value).toBe('预算 3000');

    const forced = await wb({
      subject_id: 'cust-c1', key: 'budget', value: '预算 3000', confidence: 0.7, force: true, actor: 'ops',
    });
    expect(forced.body.action).toBe('override');
    expect(forced.body.changed).toBe(true);
    expect(forced.body.value).toBe('预算 3000');

    // 裁决完就不该再挂在待裁决列表里，否则人工会反复处理同一条
    const after = await request(app()).get('/api/memory/handoff/profiles/conflicts');
    expect(after.body.find((x: { subject_id: string }) => x.subject_id === 'cust-c1')).toBeFalsy();

    const p = await request(app()).get('/api/memory/handoff/profiles/cust-c1');
    expect(p.body.profile[0].value).toBe('预算 3000');
    expect(p.body.history.some((h: { action: string; actor: string }) => h.action === 'override' && h.actor === 'ops')).toBe(true);
  });

  it('force 但值没变时记 keep，不伪造一次覆盖', async () => {
    await wb({ subject_id: 'cust-c2', key: 'industry', value: '烘焙', confidence: 0.8 });
    const same = await wb({ subject_id: 'cust-c2', key: 'industry', value: '烘焙', confidence: 0.8, force: true });
    expect(same.body.action).toBe('keep');
    expect(same.body.changed).toBe(false);
  });
});

describe('脏输入不 5xx', () => {
  it('空 body / 同人交接 / 缺 to_agent / 缺 agent / 越界置信度 / 空反馈 / 不存在的 id', async () => {
    const cases: Array<[string, string, Record<string, unknown> | null]> = [
      ['post', '/api/memory/handoff', {}],
      ['post', '/api/memory/handoff', { shift_id: 's', from_agent: 'A', to_agent: 'A' }],
      ['post', `/api/memory/handoff/${handoffId}/accept`, {}],
      ['get', '/api/memory/handoff/context', null],
      ['post', '/api/memory/handoff/profiles/writeback', {
        subject_id: 'c', key: 'k', value: 'v', confidence: 1.5,
      }],
      ['post', '/api/memory/handoff/profiles/ingest', { subject_id: 'c', text: '' }],
      ['get', '/api/memory/handoff/no-such', null],
    ];
    for (const [method, url, body] of cases) {
      const call = method === 'post' ? request(app()).post(url) : request(app()).get(url);
      const res = body ? await call.send(body) : await call;
      expect(res.status).toBeLessThan(500);
      expect(res.body?.error?.code).toBeTruthy();
    }
  });
});

describe('画像流水滚动裁剪（append-only 也要有上界）', () => {
  it('同一主体写回 250 次：流水只留最近 200 条，当前值不受影响', async () => {
    const subject = 'cust-bounded-history';
    for (let i = 1; i <= 250; i++) {
      const res = await request(app())
        .post('/api/memory/handoff/profiles/writeback')
        .send({
          subject_type: 'customer',
          subject_id: subject,
          key: 'budget',
          // 严格递增的置信度（i/251 恒小于 1）→ 每次都走 update，值会跟着升
          value: `budget-${i}`,
          confidence: i / 251,
          source: 'test',
        });
      expect(res.status).toBe(200);
    }

    const rows = sqlite
      .getDb()
      .prepare('SELECT COUNT(*) AS n FROM memory_profile_events WHERE subject_id = ?')
      .get(subject) as { n: number };
    expect(rows.n).toBe(200);

    // 当前值仍然是最新那次（裁剪动的是历史，不是画像）
    const cur = sqlite
      .getDb()
      .prepare(
        `SELECT value FROM memory_profile_values
          WHERE subject_type = 'customer' AND subject_id = ? AND key = 'budget'`,
      )
      .get(subject) as { value: string };
    expect(cur.value).toBe('budget-250');

    // 留下的是最新的：最老那条（budget-1）应该已经被裁掉
    const oldest = sqlite
      .getDb()
      .prepare('SELECT old_value, new_value FROM memory_profile_events WHERE subject_id = ? ORDER BY id ASC LIMIT 1')
      .get(subject) as { old_value: string | null; new_value: string };
    expect(oldest.new_value).not.toBe('budget-1');
  });

  it('不同主体各自独立计数，不互相挤掉', async () => {
    for (const subject of ['cust-a-bounded', 'cust-b-bounded']) {
      for (let i = 1; i <= 210; i++) {
        await request(app())
          .post('/api/memory/handoff/profiles/writeback')
          .send({
            subject_type: 'customer',
            subject_id: subject,
            key: 'tone',
            value: `tone-${i}`,
            confidence: i / 251,
            source: 'test',
          });
      }
    }
    const n = sqlite
      .getDb()
      .prepare(
        `SELECT subject_id, COUNT(*) AS n FROM memory_profile_events
          WHERE subject_id IN ('cust-a-bounded','cust-b-bounded') GROUP BY subject_id`,
      )
      .all() as Array<{ subject_id: string; n: number }>;
    expect(n.length).toBe(2);
    for (const row of n) expect(row.n).toBe(200);
  });
});

describe('画像冲突裁决闭环（发现 → 采纳 / 驳回 → 列表清空）', () => {
  const wb = (payload: Record<string, unknown>) =>
    request(app()).post('/api/memory/handoff/profiles/writeback').send(payload);
  const resolve = (payload: Record<string, unknown>) =>
    request(app()).post('/api/memory/handoff/profiles/conflicts/resolve').send(payload);
  const conflicts = () => request(app()).get('/api/memory/handoff/profiles/conflicts');
  const stats = () => request(app()).get('/api/memory/handoff/stats');
  const has = (rows: Array<{ subject_id: string }>, subject: string) =>
    rows.some((r) => r.subject_id === subject);

  const seed = async (subject: string) => {
    await wb({ subject_id: subject, key: 'budget', value: '预算 800', confidence: 0.7, source: 'manual' });
    const conflict = await wb({
      subject_id: subject, key: 'budget', value: '预算 3000', confidence: 0.7,
      source: 'feedback', evidence: '客户后来改口',
    });
    expect(conflict.body.action).toBe('conflict');
    expect(has((await conflicts()).body, subject)).toBe(true);
  };

  it('采纳提议：覆盖当前值、留 override 痕迹、冲突出列、重复裁决 404', async () => {
    await seed('cust-r1');
    const before = (await stats()).body.pending_conflicts;

    const res = await resolve({
      subject_id: 'cust-r1', key: 'budget', decision: 'accept', actor: 'ops',
    });
    expect(res.status).toBe(200);
    expect(res.body.action).toBe('override');
    expect(res.body.value).toBe('预算 3000');
    expect(res.body.actor).toBe('ops');

    const p = await request(app()).get('/api/memory/handoff/profiles/cust-r1');
    expect(p.body.profile[0].value).toBe('预算 3000');
    expect(p.body.conflicts).toBe(1);          // 历史累计仍留痕
    expect(p.body.pending_conflicts).toBe(0);  // 但不用再处理
    expect(p.body.history.some((h: { action: string; actor: string }) =>
      h.action === 'override' && h.actor === 'ops')).toBe(true);

    expect(has((await conflicts()).body, 'cust-r1')).toBe(false);
    expect((await stats()).body.pending_conflicts).toBe(before - 1);
    const again = await resolve({ subject_id: 'cust-r1', key: 'budget', decision: 'accept' });
    expect(again.status).toBe(404);
  });

  it('驳回提议：当前值不动、留 dismiss 痕迹，列表与统计同步清空', async () => {
    await seed('cust-r2');
    const before = (await stats()).body.pending_conflicts;

    const res = await resolve({
      subject_id: 'cust-r2', key: 'budget', decision: 'dismiss', actor: 'ops2',
    });
    expect(res.status).toBe(200);
    expect(res.body.action).toBe('dismiss');
    expect(res.body.value).toBe('预算 800');

    const p = await request(app()).get('/api/memory/handoff/profiles/cust-r2');
    expect(p.body.profile[0].value).toBe('预算 800');
    expect(p.body.conflicts).toBe(1);
    expect(p.body.pending_conflicts).toBe(0);
    expect(p.body.history.some((h: { action: string; actor: string }) =>
      h.action === 'dismiss' && h.actor === 'ops2')).toBe(true);

    expect(has((await conflicts()).body, 'cust-r2')).toBe(false);
    expect((await stats()).body.pending_conflicts).toBe(before - 1);
  });

  it('冲突之后又来更高置信写回：旧提议自动失效，不再挂着让人重复处理', async () => {
    await seed('cust-r3');
    await wb({ subject_id: 'cust-r3', key: 'budget', value: '预算 5000', confidence: 0.95 });
    expect(has((await conflicts()).body, 'cust-r3')).toBe(false);
    const stale = await resolve({ subject_id: 'cust-r3', key: 'budget', decision: 'accept' });
    expect(stale.status).toBe(404);
  });

  it('非法 decision / 缺字段 / 不存在的冲突一律 4xx，不是 5xx', async () => {
    const cases = [
      { subject_id: 'cust-r1', key: 'budget', decision: 'maybe' },
      { subject_id: '', key: 'budget', decision: 'accept' },
      { subject_id: 'nobody', key: 'none', decision: 'accept' },
    ];
    for (const payload of cases) {
      const res = await resolve(payload);
      expect(res.status).toBeGreaterThanOrEqual(400);
      expect(res.status).toBeLessThan(500);
      expect(res.body.error.code).toBeTruthy();
    }
  });
});

describe('反馈自动写回队列（投递即写回 → 幂等 → 冲突 → 补跑）', () => {
  const send = (body: Record<string, unknown>) =>
    request(app()).post('/api/memory/handoff/feedback').send(body);
  const queue = (qs = '') => request(app()).get(`/api/memory/handoff/feedback${qs}`);
  const profileOf = (id: string) => request(app()).get(`/api/memory/handoff/profiles/${id}`);
  const valueOf = async (id: string, key: string): Promise<string> => {
    const p = await profileOf(id);
    const rows = p.body.profile as Array<{ key: string; value: string }>;
    return rows.find((x) => x.key === key)?.value ?? '';
  };

  it('单条投递即自动写回：画像只存命中短句，回执带每个 key 的动作与来源留痕', async () => {
    const res = await send({
      subject_id: 'cust-q1',
      text: '客户偏好简洁一点，预算 800 以内',
      source: 'crm',
      source_ref: 'ticket-1',
    });
    expect(res.status).toBe(201);
    expect(res.body.processed).toBe(1);
    expect(res.body.items[0].profile_keys).toEqual(expect.arrayContaining(['preference', 'budget']));
    expect(res.body.items[0].actions).toEqual(['create', 'create']);
    expect(await valueOf('cust-q1', 'preference')).toBe('客户偏好简洁一点');
    expect(await valueOf('cust-q1', 'budget')).toBe('预算 800 以内');
    const row = (await queue('?status=processed&limit=100')).body.items.find(
      (x: { subject_id: string }) => x.subject_id === 'cust-q1',
    );
    expect(row.source).toBe('crm');
    expect(row.source_ref).toBe('ticket-1');
  });

  it('同一内容重投只入队一次，画像事件不新增', async () => {
    const before = (await profileOf('cust-q1')).body.writes as number;
    const res = await send({
      subject_id: 'cust-q1', text: '客户偏好简洁一点，预算 800 以内', source: 'crm',
    });
    expect(res.status).toBe(201);
    expect(res.body.deduped).toBe(1);
    expect(res.body.items[0].dedup).toBe(true);
    expect((await profileOf('cust-q1')).body.writes).toBe(before);
  });

  it('没命中规则的反馈记 skipped，不往画像里塞东西', async () => {
    const res = await send({ subject_id: 'cust-q2', text: '今天天气不错' });
    expect(res.body.skipped).toBe(1);
    expect(res.body.items[0].profile_keys).toEqual([]);
    expect((await profileOf('cust-q2')).body.writes).toBe(0);
  });

  it('批量投递逐条独立：一条脏数据只让自己 failed，整批仍 2xx', async () => {
    const res = await send({
      items: [
        { subject_id: 'cust-q3', text: '偏好简洁' },
        { subject_id: 'cust-q4', text: '' },
        { subject_id: '', text: '预算 900' },
        { subject_id: 'cust-q5', text: '行业是烘焙' },
      ],
    });
    expect(res.status).toBe(201);
    expect(res.body.received).toBe(4);
    expect(res.body.processed).toBe(2);
    expect(res.body.failed).toBe(2);
    expect(res.body.items[1].error).toBeTruthy();
  });

  it('同主体不同说法自动记冲突并进待裁决列表，但不擅自覆盖原值', async () => {
    await send({ subject_id: 'cust-q6', text: '预算 800 以内' });
    const second = await send({ subject_id: 'cust-q6', text: '预算 3000 以内' });
    expect(second.body.items[0].actions).toContain('conflict');
    expect(second.body.conflicts).toBe(1);
    expect(await valueOf('cust-q6', 'budget')).toBe('预算 800 以内');
    const list = await request(app()).get('/api/memory/handoff/profiles/conflicts');
    expect(
      list.body.some((x: { subject_id: string; key: string }) => x.subject_id === 'cust-q6' && x.key === 'budget'),
    ).toBe(true);
  });

  it('老入口 /profiles/ingest 走同一队列：返回 feedback_id，重投命中去重', async () => {
    const url = '/api/memory/handoff/profiles/ingest';
    const first = await request(app()).post(url).send({ subject_id: 'cust-q7', text: '偏好极简' });
    expect(first.status).toBe(200);
    expect(first.body.feedback_id).toBeTruthy();
    const again = await request(app()).post(url).send({ subject_id: 'cust-q7', text: '偏好极简' });
    expect(again.body.dedup).toBe(true);
  });

  it('队列审计：状态筛选、统计自洽、非法状态不报错', async () => {
    const all = await queue('?limit=100');
    expect(all.status).toBe(200);
    expect(all.body.stats.total).toBeGreaterThanOrEqual(6);
    const s = all.body.stats;
    expect(s.pending + s.processed + s.skipped + s.failed).toBe(s.total);
    const skipped = await queue('?status=skipped&limit=100');
    expect(skipped.body.items.every((x: { status: string }) => x.status === 'skipped')).toBe(true);
    expect((await queue('?status=nope')).status).toBe(200);
  });

  it('补跑：把一条置回 failed 后 run 能重新结算，画像值不被改写', async () => {
    const before = await valueOf('cust-q1', 'preference');
    sqlite.getDb()
      .prepare(
        `UPDATE memory_feedback_queue SET status = 'failed'
          WHERE id = (SELECT id FROM memory_feedback_queue
                       WHERE subject_id = 'cust-q1' ORDER BY created_at DESC LIMIT 1)`,
      )
      .run();
    const run = await request(app())
      .post('/api/memory/handoff/feedback/run')
      .send({ include_failed: true, limit: 100 });
    expect(run.status).toBe(200);
    expect(run.body.scanned).toBeGreaterThanOrEqual(1);
    expect(run.body.processed).toBeGreaterThanOrEqual(1);
    expect(await valueOf('cust-q1', 'preference')).toBe(before);
    expect((await queue('?limit=100')).body.stats.failed).toBe(0);
  });

  it('stats 暴露队列计数；空批量 / 超 50 条 / 奇怪 run body 一律 4xx 不是 5xx', async () => {
    const s = await request(app()).get('/api/memory/handoff/stats');
    expect(s.body.feedback_total).toBeGreaterThan(0);
    expect(s.body.feedback_pending).toBe(0);
    expect(s.body.feedback_failed).toBe(0);

    const empty = await send({ items: [] });
    expect(empty.status).toBe(400);
    expect(empty.body.error.code).toBe('INVALID_BODY');
    const tooMany = await send({
      items: Array.from({ length: 51 }, () => ({ subject_id: 'x', text: '偏好简洁' })),
    });
    expect(tooMany.status).toBe(400);
    const weirdRun = await request(app())
      .post('/api/memory/handoff/feedback/run')
      .send({ limit: 'oops', include_failed: 'yes' });
    expect(weirdRun.status).toBeLessThan(500);
  });
});

describe('记忆消费留痕（每次读上下文落一条访问记录）', () => {
  it('contextFor 落痕：消费方 / 作用域 / 带出条数 / 来源可核，stats 计数同步', async () => {
    const before = (await request(app()).get('/api/memory/handoff/context/reads?limit=100')).body;
    const read = await request(app()).get(
      '/api/memory/handoff/context?agent=消费岗-B&scope_key=copywriting&task_type=writer',
    );
    expect(read.status).toBe(200);
    expect(read.body.count).toBeGreaterThanOrEqual(1);

    const after = (await request(app()).get('/api/memory/handoff/context/reads?limit=100')).body;
    expect(after.total).toBe(before.total + 1);
    const latest = after.items[after.items.length - 1];
    expect(latest.agent).toBe('消费岗-B');
    expect(latest.scope_key).toBe('copywriting');
    expect(latest.task_type).toBe('writer');
    expect(latest.item_count).toBe(read.body.count);
    expect(latest.sources).toEqual(read.body.sources);

    const stats = (await request(app()).get('/api/memory/handoff/stats')).body;
    expect(stats.context_reads).toBe(after.total);
  });

  it('消费审计可按窗口筛；脏参数回落默认值不 5xx', async () => {
    const future = Date.now() + 60000;
    const none = (await request(app()).get(`/api/memory/handoff/context/reads?from=${future}`)).body;
    expect(none.total).toBe(0);
    expect(none.items).toEqual([]);

    const all = (await request(app()).get('/api/memory/handoff/context/reads')).body;
    expect(all.total).toBeGreaterThanOrEqual(1);
    expect(Object.keys(all.by_agent).length).toBeGreaterThanOrEqual(1);

    // limit / from / to 全给脏值：回落默认（不筛、不超上限），不是 5xx 也不是静默报错
    const dirty = await request(app()).get('/api/memory/handoff/context/reads?limit=oops&from=xx&to=yy');
    expect(dirty.status).toBe(200);
    expect(dirty.body.total).toBe(all.total);
  });

  it('访问日志滚动裁剪：连读 210 次全表只留最近 200 条，最近一次还在', async () => {
    for (let i = 0; i < 210; i++) {
      await request(app()).get('/api/memory/handoff/context?agent=压测岗&limit=1');
    }
    const total = (
      sqlite.getDb().prepare(`SELECT COUNT(*) AS n FROM memory_context_reads`).get() as { n: number }
    ).n;
    expect(total).toBe(200); // 与画像流水同法：append-only 也要有上界
    const rows = (await request(app()).get('/api/memory/handoff/context/reads?limit=1')).body;
    expect(rows.items[rows.items.length - 1].agent).toBe('压测岗');
    expect(rows.total).toBe(200);
  });
});
