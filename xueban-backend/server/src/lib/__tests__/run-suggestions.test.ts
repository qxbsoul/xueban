/**
 * run-suggestions.test.ts — lib/run-suggestions 纯函数 + LLM 编排单测。
 *
 * 覆盖：
 *   - parseSuggestions：JSON 数组 / markdown 列表 / 编号列表 / 去重 / 截 4 条 /
 *     超长截断 / 垃圾输出 → 空数组；
 *   - suggestionContextFromTimeline：goal（首个 user_turn）/ 三类产物卡去重 /
 *     summary（最后一条 assistant_text 首行）；
 *   - generateRunSuggestions：注入 callImpl——正常 / error chunk / 抛错 /
 *     解析为空 → null（绝不返回假建议）。
 */
import { describe, expect, it } from 'vitest';
import type { ProviderChunk } from '../../transport/api-clients';
import type { TimelineMessage } from '../contracts';
import {
  buildSuggestionPrompts,
  generateRunSuggestions,
  parseSuggestions,
  suggestionContextFromTimeline,
  type RunSuggestionContext,
} from '../run-suggestions';

const CTX: RunSuggestionContext = {
  goal: '写一份氢能政策简报',
  artifacts: ['氢能政策简报.md'],
  summary: '简报已完成，覆盖三条补贴政策。',
};

function msg(kind: TimelineMessage['kind'], extra: Record<string, unknown>): TimelineMessage {
  return { id: Math.random().toString(36).slice(2), kind, turn_id: 't1', ts: 1, ...extra } as TimelineMessage;
}

describe('parseSuggestions', () => {
  it('JSON 数组优先', () => {
    expect(parseSuggestions('["把简报导出为 PDF","补充申报截止日期"]')).toEqual([
      '把简报导出为 PDF',
      '补充申报截止日期',
    ]);
  });

  it('JSON 外裹文字时提取数组段', () => {
    expect(parseSuggestions('好的：\n["补充甲","补充乙"]\n以上。')).toEqual(['补充甲', '补充乙']);
  });

  it('markdown 列表 / 编号列表行解析', () => {
    expect(parseSuggestions('- 补充甲\n* 补充乙\n1. 补充丙\n2、补充丁')).toEqual(['补充甲', '补充乙', '补充丙', '补充丁']);
  });

  it('去重 + 截到 4 条', () => {
    expect(parseSuggestions('["补充甲","补充甲","补充乙","补充丙","补充丁","补充戊"]')).toEqual([
      '补充甲', '补充乙', '补充丙', '补充丁',
    ]);
  });

  it('单条超长截断到 60 字', () => {
    const long = '一'.repeat(80);
    const [first] = parseSuggestions(`["${long}"]`);
    expect(first!.length).toBe(61); // 60 + …
    expect(first!.endsWith('…')).toBe(true);
  });

  it('垃圾输出 / 空字符串 → 空数组', () => {
    expect(parseSuggestions('')).toEqual([]);
    expect(parseSuggestions('好的\n\n- \n-')).toEqual([]);
  });
});

describe('suggestionContextFromTimeline', () => {
  it('goal 取首个 user_turn；产物三类卡去重；summary 取末条 assistant 首行', () => {
    const ctx = suggestionContextFromTimeline([
      msg('user_turn', { text: '  做个调研  ' }),
      msg('user_turn', { text: '第二轮追问' }),
      msg('artifact_card', { title: '报告.md', artifact_type: 'markdown', filename: 'report.md' }),
      msg('artifact_card', { title: '报告.md', artifact_type: 'markdown', filename: 'report.md' }),
      msg('file_diff', { path: 'src/a.ts', added: 1, removed: 0, lines: [] }),
      msg('commit_record', {
        repo: 'r', sha: 'x', short_sha: 'x', message: 'm', author: 'a',
        files_changed: 1, insertions: 1, deletions: 0,
        files: [{ path: 'src/b.ts', additions: 1, deletions: 0 }],
      }),
      msg('assistant_text', { body: '第一条回复' }),
      msg('assistant_text', { body: '最终回复首行\n第二行细节' }),
    ]);
    expect(ctx.goal).toBe('做个调研');
    expect(ctx.artifacts).toEqual(['报告.md', 'src/a.ts', 'src/b.ts']);
    expect(ctx.summary).toBe('最终回复首行');
  });

  it('空时间线 → 空 goal / 空产物 / 无 summary', () => {
    const ctx = suggestionContextFromTimeline([]);
    expect(ctx).toEqual({ goal: '', artifacts: [] });
  });
});

describe('buildSuggestionPrompts', () => {
  it('user prompt 含 goal / 产物 / 摘要，system 约束 JSON 数组输出', () => {
    const { systemPrompt, userMessage } = buildSuggestionPrompts(CTX);
    expect(systemPrompt).toContain('JSON');
    expect(userMessage).toContain('写一份氢能政策简报');
    expect(userMessage).toContain('氢能政策简报.md');
    expect(userMessage).toContain('简报已完成');
  });
});

describe('generateRunSuggestions（callImpl 注入）', () => {
  async function* ok(): AsyncGenerator<ProviderChunk> {
    yield { type: 'text-delta', text: '["把简报导出为 PDF","补充申报截止日期"]' };
    yield { type: 'end' };
  }

  it('正常 → 解析后的建议数组', async () => {
    const out = await generateRunSuggestions({ ctx: CTX, provider: 'zhipu', callImpl: ok });
    expect(out).toEqual(['把简报导出为 PDF', '补充申报截止日期']);
  });

  it('error chunk → null', async () => {
    async function* err(): AsyncGenerator<ProviderChunk> {
      yield { type: 'error', message: 'no key', code: 'NO_API_KEY' };
    }
    expect(await generateRunSuggestions({ ctx: CTX, provider: 'zhipu', callImpl: err })).toBeNull();
  });

  it('callImpl 抛错 → null', async () => {
    const boom = (() => {
      throw new Error('network');
    }) as unknown as typeof ok;
    expect(await generateRunSuggestions({ ctx: CTX, provider: 'zhipu', callImpl: boom })).toBeNull();
  });

  it('模型输出解析为空 → null（不返回假建议）', async () => {
    async function* empty(): AsyncGenerator<ProviderChunk> {
      yield { type: 'text-delta', text: '无话可说' };
      yield { type: 'end' };
    }
    expect(await generateRunSuggestions({ ctx: CTX, provider: 'zhipu', callImpl: empty })).toBeNull();
  });
});
