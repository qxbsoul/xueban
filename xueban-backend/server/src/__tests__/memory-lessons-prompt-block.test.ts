/**
 * memory-lessons-prompt-block.test.ts — promptBlock 统一入口（2026-11-07 记忆闭环）。
 *
 * 背景：经验记忆进提示词的「开关/条数/格式」此前在 workflow/executor.ts 与
 * routes/run-sessions.ts 各有一份内联拷贝，聊天面（groups-chat）则完全没有
 * 召回。抽成 storage/memory-lessons.promptBlock 后三处共用，本文件锁语义：
 *   - 单域：与旧内联实现逐字一致（标题、通用条目、跨域不串、topK、开关）
 *   - 多域：合并召回按 id 去重（通用条目天然双域命中）、整体按
 *     score/hits/updated_at 排序、总量截 topK
 *   - recallTopK 1..12 截断；autoLearn=false 不召回；空库返回 ''
 *
 * Run:
 *   npx vitest run --project=server server/src/__tests__/memory-lessons-prompt-block.test.ts
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let sqlite: typeof import('../storage/sqlite');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let lessons: typeof import('../storage/memory-lessons');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let settings: typeof import('../storage/settings');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-lessons-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  sqlite.getDb();
  lessons = await import('../storage/memory-lessons');
  settings = await import('../storage/settings');
});

afterAll(() => {
  process.chdir(origCwd);
  try { rmSync(tmp, { recursive: true, force: true }); } catch { /* best-effort */ }
});

beforeEach(() => {
  sqlite.getDb().prepare('DELETE FROM memory_lessons').run();
  settings.deleteSetting('memory.recallTopK');
  settings.deleteSetting('memory.autoLearn');
});

/** 统计块里的条目行（"- [xxx]" 开头）。 */
function countRows(block: string): number {
  return block.split('\n').filter((l) => l.startsWith('- [')).length;
}

describe('promptBlock — 单域（旧内联语义逐字对齐）', () => {
  it('空库 → \'\'（无标题、无废话）', () => {
    expect(lessons.promptBlock('any-scope')).toBe('');
  });

  it('通用 + 同域进块，跨域不串；标题「## 过往经验」', () => {
    lessons.remember({ content: '交付必须附证据链', scope_key: '', kind: 'lesson', score: 0.9 });
    lessons.remember({ content: '烘焙文案要带口味词', scope_key: 'copywriting', kind: 'lesson', score: 0.8 });
    lessons.remember({ content: '出海只看东南亚', scope_key: 'overseas', kind: 'lesson', score: 0.95 });
    const block = lessons.promptBlock('copywriting');
    expect(block).toContain('## 过往经验（自动沉淀，供参考）');
    expect(block).toContain('交付必须附证据链');
    expect(block).toContain('烘焙文案要带口味词');
    expect(block).not.toContain('出海只看东南亚');
  });

  it('recallTopK=1 → 只带 1 条（按 score 最高）', () => {
    lessons.remember({ content: '低分条', scope_key: 's1', kind: 'lesson', score: 0.1 });
    lessons.remember({ content: '高分条', scope_key: 's1', kind: 'lesson', score: 0.9 });
    settings.setSetting('memory.recallTopK', 1);
    const block = lessons.promptBlock('s1');
    expect(countRows(block)).toBe(1);
    expect(block).toContain('高分条');
    expect(block).not.toContain('低分条');
  });

  it('recallTopK=0（非法）→ 回落默认 4', () => {
    for (let i = 0; i < 5; i++) {
      lessons.remember({ content: `条目${i}`, scope_key: 's2', kind: 'lesson', score: 0.5 + i * 0.01 });
    }
    settings.setSetting('memory.recallTopK', 0);
    expect(countRows(lessons.promptBlock('s2'))).toBe(4);
  });

  it('autoLearn=false → 不召回（即便库里有）', () => {
    lessons.remember({ content: '不该出现', scope_key: 's3', kind: 'lesson', score: 0.9 });
    settings.setSetting('memory.autoLearn', false);
    expect(lessons.promptBlock('s3')).toBe('');
  });
});

describe('promptBlock — 多域合并（聊天召回用）', () => {
  it('两域 + 通用合并去重：通用只出现一次，跨域不进', () => {
    lessons.remember({ content: '通用经验', scope_key: '', kind: 'lesson', score: 0.9 });
    lessons.remember({ content: 'A 域经验', scope_key: 'agent-a', kind: 'lesson', score: 0.8 });
    lessons.remember({ content: '聊天域经验', scope_key: 'chat-gateway', kind: 'lesson', score: 0.85 });
    lessons.remember({ content: '无关域经验', scope_key: 'writer', kind: 'lesson', score: 0.99 });
    const block = lessons.promptBlock(['agent-a', 'chat-gateway']);
    expect(block).toContain('通用经验');
    expect(block).toContain('A 域经验');
    expect(block).toContain('聊天域经验');
    expect(block).not.toContain('无关域经验');
    // 去重：通用条目只一行
    expect(countRows(block)).toBe(3);
    expect(block.split('通用经验').length - 1).toBe(1);
  });

  it('合并后整体按 score 排序、总量截 topK', () => {
    lessons.remember({ content: 'A1 高分', scope_key: 'agent-a', kind: 'lesson', score: 0.95 });
    lessons.remember({ content: 'A2 低分', scope_key: 'agent-a', kind: 'lesson', score: 0.2 });
    lessons.remember({ content: 'G1 中分', scope_key: 'chat-gateway', kind: 'lesson', score: 0.6 });
    lessons.remember({ content: 'G2 中低分', scope_key: 'chat-gateway', kind: 'lesson', score: 0.4 });
    settings.setSetting('memory.recallTopK', 2);
    const block = lessons.promptBlock(['agent-a', 'chat-gateway']);
    expect(countRows(block)).toBe(2);
    expect(block).toContain('A1 高分');
    expect(block).toContain('G1 中分');
    expect(block).not.toContain('A2 低分');
    expect(block).not.toContain('G2 中低分');
  });

  it('recallTopK 超上限 12 → 最多 12 条（防记忆塞满提示词）', () => {
    // 每域 8 条（MAX_PER_SCOPE 内不触发归档），两域合并 16 → 截 12。
    for (let i = 0; i < 8; i++) {
      lessons.remember({ content: `a${i}`, scope_key: 'agent-a', kind: 'lesson', score: 0.5 });
      lessons.remember({ content: `g${i}`, scope_key: 'chat-gateway', kind: 'lesson', score: 0.5 });
    }
    settings.setSetting('memory.recallTopK', 999);
    expect(countRows(lessons.promptBlock(['agent-a', 'chat-gateway']))).toBe(12);
  });

  it('autoLearn=false → 多域同样不召回', () => {
    lessons.remember({ content: '不该出现', scope_key: 'chat-gateway', kind: 'lesson', score: 0.9 });
    settings.setSetting('memory.autoLearn', false);
    expect(lessons.promptBlock(['agent-a', 'chat-gateway'])).toBe('');
  });
});
