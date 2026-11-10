/**
 * R2（2026-06-24）— createToolCallProjector 映射契约：run-sessions 把网关的
 * tool-use / tool-result SSE payload → 归一化 `tool-call` 帧（StepList 在步骤下
 * 嵌套渲染，clawsgo 左栏「搜索词 → 找到 N 个结果 → 可点链接」节拍）。
 *
 * 镜像 emit-tool-chunk.test.ts 的风格，锁住：
 *   - running → done 状态翻转
 *   - id 配对（tool-use 的 id ↔ tool-result 的 for）
 *   - 链接抽取（http(s) URL 去重 + host/path label）
 *   - elapsed（注入 now() → 确定性耗时）
 *   - result_count（clawsgo「找到 N 个结果」，从结果文本解析；无短语回退链接数）
 *   - node_id 透传（team/DAG 多 agent 按 node_id 分桶）
 *   - is_error → status:'failed'
 *
 * 纯函数 + 可注入 now()，无需起 HTTP / SSE。
 */
import { describe, it, expect } from 'vitest';
import {
  createToolCallProjector,
  parseResultCount,
  extractToolLinks,
  summarizeToolInput,
} from '../run-sessions';

describe('createToolCallProjector (R2 工具可见 · 左栏投影)', () => {
  it('tool-use → tool-call(running) with tool + title + input_summary', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    const running = p.onToolUse(
      { name: 'web_search', input: { query: 'xueban docs' }, id: 'toolu_1' },
      2,
    );
    expect(running).toBeTruthy();
    expect(running!.id).toBe('toolu_1');
    expect(running!.tool).toBe('web_search');
    expect(running!.status).toBe('running');
    // input_summary prefers the `query` field for search tools.
    expect(running!.input_summary).toBe('query: xueban docs');
    expect(running!.title).toBe('query: xueban docs');
    // step_index carried from the currently-open step.
    expect(running!.step_index).toBe(2);
    // running frame has no result yet.
    expect(running!.result_summary).toBeUndefined();
    expect(running!.elapsed_ms).toBeUndefined();
  });

  it('tool-use → tool-result 配对：running→done 翻转 + id 配对 + elapsed', () => {
    let t = 1000;
    const p = createToolCallProjector({ idSeed: 'sess01', now: () => t });
    const running = p.onToolUse({ name: 'grep', input: { q: 'x' }, id: 'toolu_2' }, 0);
    expect(running!.status).toBe('running');
    // advance the injected clock by 250ms before the result lands.
    t = 1250;
    const done = p.onToolResult({ for: 'toolu_2', output: 'found 3 matches' });
    expect(done).toBeTruthy();
    // id pairing — result reuses the tool-use call id.
    expect(done!.id).toBe('toolu_2');
    // status flipped running → done.
    expect(done!.status).toBe('done');
    // tool name preserved from the in-flight running frame.
    expect(done!.tool).toBe('grep');
    // elapsed derived from injected clock (deterministic).
    expect(done!.elapsed_ms).toBe(250);
    // step_index preserved from the running frame.
    expect(done!.step_index).toBe(0);
  });

  it('链接抽取：result 里的 http(s) URL → links(host/path label, 去重)', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    p.onToolUse({ name: 'web_search', input: { query: 'x' }, id: 'toolu_3' }, 1);
    const done = p.onToolResult({
      for: 'toolu_3',
      output:
        'found 2 results\n' +
        'https://www.example.com/a/b\n' +
        'https://docs.foo.io/guide\n' +
        'https://www.example.com/a/b', // duplicate → deduped
    });
    expect(done!.links).toBeTruthy();
    expect(done!.links).toHaveLength(2);
    // www. stripped, host + path used as label.
    expect(done!.links![0]).toEqual({ label: 'example.com/a/b', url: 'https://www.example.com/a/b' });
    expect(done!.links![1]).toEqual({ label: 'docs.foo.io/guide', url: 'https://docs.foo.io/guide' });
  });

  it('result_count：从结果短语解析「找到 N 个结果」（优先于链接数）', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    p.onToolUse({ name: 'web_search', input: { query: 'x' }, id: 'toolu_4' }, 1);
    // 12 announced hits but only 2 URLs surfaced → count must reflect the real 12.
    const done = p.onToolResult({
      for: 'toolu_4',
      output: 'found 12 results: https://a.com https://b.com',
    });
    expect(done!.result_count).toBe(12);
    expect(done!.links).toHaveLength(2);
  });

  it('result_count：无短语时回退到链接数', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    p.onToolUse({ name: 'fetch', input: { url: 'x' }, id: 'toolu_5' }, 1);
    const done = p.onToolResult({
      for: 'toolu_5',
      output: 'see https://only-one.com/page for details',
    });
    expect(done!.result_count).toBe(1);
  });

  it('is_error:true → status:failed', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    p.onToolUse({ name: 'read_file', input: { path: 'missing.ts' }, id: 'toolu_6' }, 1);
    const done = p.onToolResult({ for: 'toolu_6', output: 'Error: ENOENT', is_error: true });
    expect(done!.status).toBe('failed');
    expect(done!.result_summary).toBe('Error: ENOENT');
  });

  it('node_id 透传：team/DAG 多 agent 按 node_id 分桶', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    const running = p.onToolUse(
      { name: 'web_search', input: { query: 'x' }, id: 'toolu_7', node_id: 'researcher' },
      1,
    );
    expect(running!.node_id).toBe('researcher');
    const done = p.onToolResult({ for: 'toolu_7', output: 'found 1 result' });
    // node_id carried over from the in-flight running frame.
    expect(done!.node_id).toBe('researcher');
  });

  it('missing tool-use id → 合成 fallback id（不崩、status running）', () => {
    const p = createToolCallProjector({ idSeed: 'abcdef' });
    const running = p.onToolUse({ name: 'tool', input: {} }, null);
    expect(running!.id).toMatch(/^tc-abcdef-\d+$/);
    expect(running!.status).toBe('running');
    // no open step → step_index omitted.
    expect(running!.step_index).toBeUndefined();
  });

  it('非对象 payload → null（不抛）', () => {
    const p = createToolCallProjector({ idSeed: 'sess01' });
    expect(p.onToolUse(null, 0)).toBeNull();
    expect(p.onToolUse('nope', 0)).toBeNull();
    expect(p.onToolResult(undefined)).toBeNull();
  });
});

describe('parseResultCount (R2「找到 N 个结果」解析)', () => {
  it('EN: found N results / N matches / N hits', () => {
    expect(parseResultCount('found 12 results')).toBe(12);
    expect(parseResultCount('3 matches in file')).toBe(3);
    expect(parseResultCount('returned 5 hits')).toBe(5);
  });

  it('ZH: 找到 N 个结果 / N 条结果 / N 个匹配 / N 行', () => {
    expect(parseResultCount('找到 8 个结果')).toBe(8);
    expect(parseResultCount('共 20 条结果')).toBe(20);
    expect(parseResultCount('3 个匹配')).toBe(3);
    expect(parseResultCount('a\nb\nc 5 行')).toBe(5);
  });

  it('千分位逗号 → 去逗号解析', () => {
    expect(parseResultCount('found 1,234 results')).toBe(1234);
  });

  it('无计数短语 → undefined', () => {
    expect(parseResultCount('just some prose without a count')).toBeUndefined();
    expect(parseResultCount('')).toBeUndefined();
    expect(parseResultCount(null)).toBeUndefined();
  });
});

describe('extractToolLinks / summarizeToolInput (R2 辅助)', () => {
  it('extractToolLinks 上限 5、去尾随标点', () => {
    const text = Array.from({ length: 8 }, (_, i) => `https://site${i}.com/x.`).join(' ');
    const links = extractToolLinks(text);
    expect(links).toHaveLength(5);
    // trailing dot stripped from the url.
    expect(links[0].url).toBe('https://site0.com/x');
  });

  it('summarizeToolInput 截断到 160', () => {
    const big = 'y'.repeat(500);
    expect(summarizeToolInput(big).length).toBe(160);
  });
});
