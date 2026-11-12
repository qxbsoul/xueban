/**
 * 不同账号之间的记忆隔离回归测试（2026-11-12）。
 *
 * 产品面向学生个人：记忆按登录账号隔离，不引入租户/学生层级。
 * 覆盖：经验记忆、交接包、画像、上下文消费都只属于当前账号。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
let sqlite: typeof import('../storage/sqlite');
let lessons: typeof import('../storage/memory-lessons');
let handoff: typeof import('../storage/memory-handoff');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-memory-isolation-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  lessons = await import('../storage/memory-lessons');
  handoff = await import('../storage/memory-handoff');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('不同账号的经验记忆隔离', () => {
  it('A 写的经验不会出现在 B 的召回里', () => {
    lessons.remember({ content: '账号 A 的专属经验', scope_key: 'math', owner_id: 'account-a' });
    lessons.remember({ content: '账号 B 的专属经验', scope_key: 'math', owner_id: 'account-b' });

    const a = lessons.recallLessons('math', '', 10, 'account-a');
    const b = lessons.recallLessons('math', '', 10, 'account-b');
    expect(a.map((x) => x.content)).toContain('账号 A 的专属经验');
    expect(a.map((x) => x.content)).not.toContain('账号 B 的专属经验');
    expect(b.map((x) => x.content)).toContain('账号 B 的专属经验');
    expect(b.map((x) => x.content)).not.toContain('账号 A 的专属经验');
  });

  it('列表 / 统计 / 删除都按账号过滤', () => {
    const a = lessons.listLessons({ owner_id: 'account-a' });
    expect(a.every((x) => x.owner_id === 'account-a')).toBe(true);
    expect(lessons.lessonStats('account-a').total).toBe(a.length);
    const target = a[0];
    expect(() => lessons.forgetLesson(target.id, 'account-b')).toThrow();
    expect(lessons.getLesson(target.id, 'account-a')?.id).toBe(target.id);
  });
});

describe('不同账号的交接与画像隔离', () => {
  it('交接快照只带当前账号的画像', () => {
    handoff.writeback({ owner_id: 'account-a', subject_type: 'student', subject_id: 'me', key: 'level', value: 'A 的画像' });
    handoff.writeback({ owner_id: 'account-b', subject_type: 'student', subject_id: 'me', key: 'level', value: 'B 的画像' });

    const hA = handoff.createHandoff({
      owner_id: 'account-a', shift_id: 'acc-1', from_agent: 'A', to_agent: 'B',
      scope_key: 'math', task_type: 'plan',
    });
    const hB = handoff.createHandoff({
      owner_id: 'account-b', shift_id: 'acc-1', from_agent: 'A', to_agent: 'B',
      scope_key: 'math', task_type: 'plan',
    });
    expect(hA.handoff.context?.profiles?.map((x) => x.value)).toEqual(['A 的画像']);
    expect(hB.handoff.context?.profiles?.map((x) => x.value)).toEqual(['B 的画像']);
  });

  it('没有账号上下文时不带任何画像', () => {
    const h = handoff.createHandoff({
      shift_id: 'acc-none', from_agent: 'A', to_agent: 'B',
      scope_key: 'math', task_type: 'plan',
    });
    expect(h.handoff.context?.profiles).toEqual([]);
  });

  it('交接包读取 / 接班 / 上下文消费都按账号隔离', () => {
    const h = handoff.createHandoff({
      owner_id: 'account-a', shift_id: 'acc-2', from_agent: 'A', to_agent: 'C',
      scope_key: 'math', task_type: 'plan', summary: '给 C 的交接',
    });
    expect(handoff.getHandoff(h.handoff.id, 'account-b')).toBeUndefined();
    expect(handoff.listHandoffs({ owner_id: 'account-b' }).some((x) => x.id === h.handoff.id)).toBe(false);
    expect(() => handoff.acceptHandoff(h.handoff.id, 'C', 'account-b')).toThrow();

    handoff.acceptHandoff(h.handoff.id, 'C', 'account-a');
    expect(handoff.contextFor('C', 'math', 'plan', 20, 'account-a').items.some((x) => x.source === `handoff:${h.handoff.id}`)).toBe(true);
    expect(handoff.contextFor('C', 'math', 'plan', 20, 'account-b').items.some((x) => x.source === `handoff:${h.handoff.id}`)).toBe(false);
  });

  it('画像冲突查询按账号关联当前值，不串到另一个账号', () => {
    handoff.writeback({ owner_id: 'account-a', subject_type: 'student', subject_id: 'me', key: 'budget', value: 'A 预算 800', confidence: 0.7 });
    handoff.writeback({ owner_id: 'account-a', subject_type: 'student', subject_id: 'me', key: 'budget', value: 'A 预算 3000', confidence: 0.7 });
    handoff.writeback({ owner_id: 'account-b', subject_type: 'student', subject_id: 'me', key: 'budget', value: 'B 预算 800', confidence: 0.7 });
    handoff.writeback({ owner_id: 'account-b', subject_type: 'student', subject_id: 'me', key: 'budget', value: 'B 预算 3000', confidence: 0.7 });

    const conflictsA = handoff.listConflicts({ owner_id: 'account-a' });
    const conflictsB = handoff.listConflicts({ owner_id: 'account-b' });
    expect(conflictsA).toHaveLength(1);
    expect(conflictsB).toHaveLength(1);
    expect(conflictsA[0].current_value).toBe('A 预算 800');
    expect(conflictsA[0].proposed_value).toBe('A 预算 3000');
    expect(conflictsB[0].current_value).toBe('B 预算 800');
    expect(conflictsB[0].proposed_value).toBe('B 预算 3000');
  });

  it('画像读写按账号隔离，同一 subject 不互相覆盖', () => {
    expect(handoff.profile('me', 'student', 'account-a').profile.find((x) => x.key === 'level')?.value).toBe('A 的画像');
    expect(handoff.profile('me', 'student', 'account-b').profile.find((x) => x.key === 'level')?.value).toBe('B 的画像');
  });
});
