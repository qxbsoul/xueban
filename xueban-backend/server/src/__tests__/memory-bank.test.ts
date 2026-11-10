/**
 * memory-bank.test.ts — 记忆库检索辅助模块契约（对话框知识库接线）。
 *
 * stubGlobal fetch 测：
 *   - coercePackIds：清洗 / 去重 / 上限 8 / 非法项丢弃
 *   - fetchMemoryBankHits：多包聚合、score 降序、单包失败跳过、坏命中清洗
 *   - renderKnowledgeBlock：来源标注、单条截断、整块截断、零命中 ''
 *   - knowledgeBlock：fail-soft（全挂返回 ''，绝不抛）
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  coercePackIds,
  fetchMemoryBankHits,
  renderKnowledgeBlock,
  knowledgeBlock,
} from '../lib/memory-bank';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

describe('coercePackIds', () => {
  it('returns [] for non-array input', () => {
    expect(coercePackIds(undefined)).toEqual([]);
    expect(coercePackIds(null)).toEqual([]);
    expect(coercePackIds('p1')).toEqual([]);
    expect(coercePackIds({ 0: 'p1' })).toEqual([]);
  });

  it('trims, drops non-strings/empty/oversized, dedupes', () => {
    expect(
      coercePackIds([' p1 ', 'p1', '', '   ', 42, null, { x: 1 }, 'p2', 'x'.repeat(201)]),
    ).toEqual(['p1', 'p2']);
  });

  it('caps at 8 ids', () => {
    const ids = Array.from({ length: 12 }, (_, i) => `p${i}`);
    expect(coercePackIds(ids)).toEqual(ids.slice(0, 8));
  });
});

describe('fetchMemoryBankHits', () => {
  it('returns [] without packs or with blank query', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    expect(await fetchMemoryBankHits([], 'q')).toEqual([]);
    expect(await fetchMemoryBankHits(['p1'], '   ')).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('queries each pack with top_k=4 and aggregates hits by score desc', async () => {
    const fetchMock = vi.fn(async (url: unknown, init?: { body?: unknown }) => {
      const u = String(url);
      if (u.includes('/packs/p1/query')) {
        return okJson({
          data: { hits: [{ text: '低分命中', source: 'a.md', score: 0.3 }] },
        });
      }
      return okJson({
        data: { hits: [{ text: '高分命中', source: 'b.md', score: 0.9 }] },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const hits = await fetchMemoryBankHits(['p1', 'p2'], '氢能');
    expect(hits.map((h) => h.text)).toEqual(['高分命中', '低分命中']);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toEqual({ query: '氢能', top_k: 4 });
  });

  it('skips failing pack but keeps others (fail-soft)', async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.includes('/packs/bad/query')) return { ok: false, status: 500, json: async () => ({}) } as Response;
      if (u.includes('/packs/boom/query')) throw new Error('ECONNREFUSED');
      return okJson({ data: { hits: [{ text: '幸存命中', source: 's', score: 1 }] } });
    });
    vi.stubGlobal('fetch', fetchMock);

    const hits = await fetchMemoryBankHits(['bad', 'boom', 'good'], 'q');
    expect(hits).toEqual([{ text: '幸存命中', source: 's', score: 1 }]);
  });

  it('cleans malformed hits: drops empty text, falls back source to packId', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        okJson({
          data: {
            hits: [
              { text: '   ', source: 's', score: 1 },
              { source: 'only-source.md', score: 1 },
              { text: 42, source: 's', score: 1 },
              { text: ' 有效 ', source: '', score: 'NaN-ish' },
            ],
          },
        }),
      ),
    );

    const hits = await fetchMemoryBankHits(['pk'], 'q');
    expect(hits).toEqual([{ text: '有效', source: 'pk', score: 0 }]);
  });
});

describe('renderKnowledgeBlock', () => {
  it("returns '' for zero hits", () => {
    expect(renderKnowledgeBlock([])).toBe('');
  });

  it('renders header with source annotation per hit', () => {
    const block = renderKnowledgeBlock([
      { text: '片段一', source: 'a.md', score: 1 },
      { text: '片段二', source: 'b.md', score: 0.5 },
    ]);
    expect(block).toContain('## 知识库参考');
    expect(block).toContain('- （来源：a.md）片段一');
    expect(block).toContain('- （来源：b.md）片段二');
  });

  it('truncates single hit text at 400 chars', () => {
    const block = renderKnowledgeBlock([{ text: 'x'.repeat(500), source: 's', score: 1 }]);
    expect(block).toContain(`${'x'.repeat(400)}…`);
    expect(block).not.toContain('x'.repeat(401) + 'x'.repeat(100));
  });

  it('caps whole block at 4000 chars with truncation marker', () => {
    const hits = Array.from({ length: 20 }, (_, i) => ({
      text: `段落${i}：${'文'.repeat(380)}`,
      source: `s${i}.md`,
      score: 1 - i * 0.01,
    }));
    const block = renderKnowledgeBlock(hits);
    expect(block.length).toBeLessThanOrEqual(4000 + '\n…（已截断）'.length);
    expect(block.endsWith('…（已截断）')).toBe(true);
  });
});

describe('knowledgeBlock', () => {
  it('returns rendered block on happy path', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okJson({ data: { hits: [{ text: '知识', source: 'k', score: 1 }] } })),
    );
    const block = await knowledgeBlock(['p1'], '问题');
    expect(block).toContain('## 知识库参考');
    expect(block).toContain('（来源：k）知识');
  });

  it('fails soft to empty string when fetch blows up', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('ECONNREFUSED'); }));
    await expect(knowledgeBlock(['p1'], 'q')).resolves.toBe('');
  });

  it('returns empty string for undefined packIds', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(knowledgeBlock(undefined, 'q')).resolves.toBe('');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
