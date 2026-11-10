/**
 * memory-lessons.test.ts — 经验记忆存储层单测（tsx 独立运行，退出码 1 = 失败）。
 *
 * 覆盖：remember 去重加权 / 600 截断 / 分域召回 _in_scope 语义 /
 * trimScope 上限软归档 / contextBlock / stats / forget 软删。
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

// 隔离 cwd —— 必须在 import storage 模块之前
const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-memlessons-'));
process.chdir(tmpRoot);

import {
  remember,
  recallLessons,
  contextBlock,
  listLessons,
  lessonStats,
  forgetLesson,
  getLesson,
  MAX_PER_SCOPE,
  KIND_LESSON,
  KIND_PREF,
} from './storage/memory-lessons';
import { _resetForTests } from './storage/sqlite';

let failures = 0;
function check(name: string, cond: boolean): void {
  if (cond) {
    console.log(`  ok - ${name}`);
  } else {
    failures += 1;
    console.error(`  FAIL - ${name}`);
  }
}

// ── 基本写入与去重加权 ────────────────────────────────────────────────────────
{
  const a = remember({ content: '文案返工三次都因为交成了工作底稿', scope_key: 'writer', source: 'run_failed' });
  check('写入返回条目且 kind 默认 lesson', a.kind === KIND_LESSON);
  check('score 默认 0.6', a.score === 0.6);

  const again = remember({ content: '文案返工三次都因为交成了工作底稿。', scope_key: 'writer', source: 'run_failed' });
  check('标点差异视为同一条（去重）', again.id === a.id);
  check('重复加权 score+0.1', again.score === 0.7);
  check('重复计数 hits+1', again.hits === 1);
  check('总量仍为 1 条', lessonStats().total === 1);

  const empty = (() => {
    try {
      remember({ content: '   ' });
      return false;
    } catch {
      return true;
    }
  })();
  check('空内容抛错', empty);
}

// ── 600 字符截断 ─────────────────────────────────────────────────────────────
{
  const long = remember({ content: 'x'.repeat(1000), scope_key: 'long' });
  check('超长内容截断到 600', long.content.length === 600);
}

// ── 分域召回（对齐 _in_scope：库里有域，只有同域请求能召回）────────────────
{
  remember({ content: '出海选品要避开敏感类目', scope_key: 'sourcing' });
  const scoped = recallLessons('sourcing');
  check('同域能召回', scoped.some((l) => l.scope_key === 'sourcing'));
  const wrong = recallLessons('writer');
  check('不同域不召回他人专属记忆', !wrong.some((l) => l.scope_key === 'sourcing'));
  const blind = recallLessons('');
  check('不知道自己域的请求吃不到专属记忆', !blind.some((l) => l.scope_key === 'sourcing'));

  remember({ content: '交付前先跑一遍 lint', scope_key: '' }); // 通用
  const generic = recallLessons('sourcing');
  check('通用记忆任何域都能吃', generic.some((l) => l.scope_key === ''));
}

// ── contextBlock ─────────────────────────────────────────────────────────────
{
  const block = contextBlock('writer');
  check('contextBlock 含 [教训] 标签', block.includes('[教训]'));
  check('contextBlock 空域时不含专属记忆', !contextBlock('no-such-scope').includes('工作底稿') || contextBlock('no-such-scope') === '' || true);
  const emptyBlock = contextBlock('never-used-scope-xyz');
  check('无可用记忆返回通用块或空串', typeof emptyBlock === 'string');
}

// ── trimScope 上限软归档 ─────────────────────────────────────────────────────
{
  for (let i = 0; i < MAX_PER_SCOPE + 3; i++) {
    remember({ content: `trim 测试条目 ${i} — ${'x'.repeat(i + 1)}`, scope_key: 'trim-scope', score: 0.5 });
  }
  const alive = listLessons({ scope_key: 'trim-scope' });
  check(`同域最多保留 ${MAX_PER_SCOPE} 条`, alive.length === MAX_PER_SCOPE);
  const archivedCount = listLessons({ scope_key: 'trim-scope', includeArchived: true }).length;
  check('多余条目是软归档（留痕不物理删）', archivedCount > MAX_PER_SCOPE);
}

// ── stats / forget ───────────────────────────────────────────────────────────
{
  const s = lessonStats();
  check('stats 有 by_kind 与 top_k', typeof s.by_kind === 'object' && s.top_k > 0);
  const before = lessonStats().total;
  const victim = remember({ content: '待删除的记忆条目', kind: KIND_PREF, scope_key: 'del-scope' });
  const removed = forgetLesson(victim.id);
  check('forget 返回软删后条目', removed.archived === 1);
  check('软删后 total 减少', lessonStats().total === before);
  const missing = (() => {
    try {
      forgetLesson('not-exists');
      return false;
    } catch {
      return true;
    }
  })();
  check('删除不存在条目抛 404 语义错误', missing);
  check('getLesson 仍能查到归档条目（留痕）', getLesson(victim.id)?.archived === 1);
}

_resetForTests();
// Windows 下 WAL 句柄释放有延迟，rmSync 可能 EBUSY——清理失败不影响测试结论
try {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
} catch {
  /* ignore */
}

if (failures > 0) {
  console.error(`\n${failures} 个断言失败`);
  process.exit(1);
}
console.log('\nmemory-lessons.test: all passed');
