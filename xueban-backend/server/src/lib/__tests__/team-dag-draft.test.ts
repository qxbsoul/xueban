/**
 * team-dag-draft.test.ts — 自然语言 → DAG 草稿的纯函数 + LLM 调用路径。
 * compose/parse/validate 全纯函数直测；generateDagDraft 用注入 callImpl mock
 * （无需真实 key / 网络），含「校验不合法 → 回喂错误重试一次」路径。
 */
import { describe, expect, it, vi } from 'vitest';
import type { ProviderChunk } from '../../transport/api-clients';
import {
  composeDagDraftPrompt,
  generateDagDraft,
  normalizeDraftEdgeMode,
  parseDagDraftJson,
  validateDagDraft,
  type DagDraft,
  type DagDraftAgent,
} from '../team-dag-draft';

const ROSTER: DagDraftAgent[] = [
  { id: 'pm', name: '产品经理', role: '需求拆解' },
  { id: 'dev', name: '开发', role: '实现' },
  { id: 'qa', name: '测试', role: '验收' },
];

const VALID_DRAFT = {
  nodes: [
    { id: 'n1', agentId: 'pm', name: '产品经理', role: '需求' },
    { id: 'n2', agentId: 'dev', name: '开发' },
  ],
  edges: [{ source: 'n1', target: 'n2', mode: 'direct' }],
};

describe('composeDagDraftPrompt', () => {
  it('嵌入 roster（id/name/role）、用户描述与输出契约', () => {
    const p = composeDagDraftPrompt('先写需求再开发', ROSTER);
    expect(p).toContain('id: pm');
    expect(p).toContain('role: 需求拆解');
    expect(p).toContain('先写需求再开发');
    expect(p).toContain('direct|approve|conditional');
  });

  it('重试时附上上一稿校验错误', () => {
    const p = composeDagDraftPrompt('x', ROSTER, ['self-loop edge "a" → "a" is not allowed']);
    expect(p).toContain('上一稿的校验错误');
    expect(p).toContain('self-loop');
  });
});

describe('normalizeDraftEdgeMode', () => {
  it('approve/conditional 严格匹配，其余收敛到 direct', () => {
    expect(normalizeDraftEdgeMode('approve')).toBe('approve');
    expect(normalizeDraftEdgeMode('conditional')).toBe('conditional');
    expect(normalizeDraftEdgeMode('direct')).toBe('direct');
    expect(normalizeDraftEdgeMode('parallel')).toBe('direct');
    expect(normalizeDraftEdgeMode(undefined)).toBe('direct');
  });
});

describe('parseDagDraftJson', () => {
  it('解析干净 JSON 对象', () => {
    const draft = parseDagDraftJson(JSON.stringify(VALID_DRAFT))!;
    expect(draft.nodes).toHaveLength(2);
    expect(draft.edges[0]).toEqual({ source: 'n1', target: 'n2', mode: 'direct' });
  });

  it('容忍 ```json 围栏与前后杂文', () => {
    const raw = `好的：\n\`\`\`json\n${JSON.stringify(VALID_DRAFT)}\n\`\`\`\n以上。`;
    const draft = parseDagDraftJson(raw)!;
    expect(draft.nodes.map((n) => n.id)).toEqual(['n1', 'n2']);
  });

  it('未知 mode 归一为 direct；重复节点 id 首个生效', () => {
    const draft = parseDagDraftJson(
      JSON.stringify({
        nodes: [{ id: 'a' }, { id: 'a', name: '重复' }, { id: 'b' }],
        edges: [{ source: 'a', target: 'b', mode: 'weird' }],
      }),
    )!;
    expect(draft.nodes).toHaveLength(2);
    expect(draft.nodes[0]!.name).toBeUndefined();
    expect(draft.edges[0]!.mode).toBe('direct');
  });

  it('空节点数组 / 非 JSON → null', () => {
    expect(parseDagDraftJson('{"nodes":[],"edges":[]}')).toBeNull();
    expect(parseDagDraftJson('not json at all')).toBeNull();
    expect(parseDagDraftJson('')).toBeNull();
  });
});

describe('validateDagDraft', () => {
  const draft = (nodes: string[], edges: DagDraft['edges']): DagDraft => ({
    nodes: nodes.map((id) => ({ id })),
    edges,
  });

  it('合法链式图 → 无错误', () => {
    expect(
      validateDagDraft(draft(['a', 'b'], [{ source: 'a', target: 'b', mode: 'direct' }])),
    ).toEqual([]);
  });

  it('自环 / 悬空端点被拒', () => {
    const errs = validateDagDraft(
      draft(['a'], [
        { source: 'a', target: 'a', mode: 'direct' },
        { source: 'a', target: 'ghost', mode: 'direct' },
      ]),
    );
    expect(errs.some((e) => e.includes('self-loop'))).toBe(true);
    expect(errs.some((e) => e.includes('ghost'))).toBe(true);
  });

  it('direct/approve 边成环被拒；conditional 回边合法', () => {
    const cyclic = validateDagDraft(
      draft(['a', 'b'], [
        { source: 'a', target: 'b', mode: 'direct' },
        { source: 'b', target: 'a', mode: 'approve' },
      ]),
    );
    expect(cyclic.some((e) => /cycle/i.test(e))).toBe(true);

    const conditionalBack = validateDagDraft(
      draft(['a', 'b'], [
        { source: 'a', target: 'b', mode: 'direct' },
        { source: 'b', target: 'a', mode: 'conditional' },
      ]),
    );
    expect(conditionalBack).toEqual([]);
  });

  it('D2：给了 roster 时 agentId 必须来自名单', () => {
    const withRoster = validateDagDraft(
      { nodes: [{ id: 'n1', agentId: 'ghost' }, { id: 'n2' }], edges: [] },
      ['pm', 'dev'],
    );
    expect(withRoster.some((e) => e.includes('ghost'))).toBe(true);
    expect(withRoster.some((e) => e.includes('no agentId'))).toBe(true);
    // 不给 roster → 跳过 D2
    expect(validateDagDraft({ nodes: [{ id: 'n1' }], edges: [] }, [])).toEqual([]);
  });
});

describe('generateDagDraft', () => {
  const gen = (text: string) =>
    async function* (): AsyncGenerator<ProviderChunk> {
      yield { type: 'text-delta', text };
      yield { type: 'end' };
    };

  const baseOpts = {
    userText: '先需求后开发',
    agents: ROSTER,
    provider: 'zhipu',
    apiKey: 'sk-test',
  };

  it('一次成功 → ok + draft', async () => {
    const callImpl = vi.fn().mockImplementation(gen(JSON.stringify(VALID_DRAFT)));
    const out = await generateDagDraft({ ...baseOpts, callImpl });
    expect(out.ok).toBe(true);
    if (out.ok) expect(out.draft.nodes).toHaveLength(2);
    expect(callImpl).toHaveBeenCalledTimes(1);
  });

  it('首稿不合法 → 回喂错误重试一次后成功', async () => {
    const badDraft = JSON.stringify({
      nodes: [{ id: 'n1', agentId: 'pm' }, { id: 'n2', agentId: 'dev' }],
      edges: [
        { source: 'n1', target: 'n2', mode: 'direct' },
        { source: 'n2', target: 'n1', mode: 'direct' },
      ],
    });
    const callImpl = vi
      .fn()
      .mockImplementationOnce(gen(badDraft))
      .mockImplementationOnce(gen(JSON.stringify(VALID_DRAFT)));
    const out = await generateDagDraft({ ...baseOpts, callImpl });
    expect(out.ok).toBe(true);
    expect(callImpl).toHaveBeenCalledTimes(2);
    // 第二次调用的 prompt 带了错误清单
    const secondPrompt = String(callImpl.mock.calls[1]![1].userMessage);
    expect(secondPrompt).toContain('cycle');
  });

  it('两次都不合法 → kind:invalid + errors', async () => {
    const badDraft = JSON.stringify({
      nodes: [{ id: 'n1', agentId: 'pm' }],
      edges: [{ source: 'n1', target: 'n1', mode: 'direct' }],
    });
    const callImpl = vi.fn().mockImplementation(gen(badDraft));
    const out = await generateDagDraft({ ...baseOpts, callImpl });
    expect(out).toMatchObject({ ok: false, kind: 'invalid' });
    if (!out.ok && out.kind === 'invalid') {
      expect(out.errors.some((e) => e.includes('self-loop'))).toBe(true);
    }
    expect(callImpl).toHaveBeenCalledTimes(2);
  });

  it('LLM 报错 → kind:llm（不重试）', async () => {
    const callImpl = vi.fn().mockImplementation(async function* (): AsyncGenerator<ProviderChunk> {
      yield { type: 'error', message: 'boom', code: 'NETWORK_ERROR' };
    });
    const out = await generateDagDraft({ ...baseOpts, callImpl });
    expect(out).toEqual({ ok: false, kind: 'llm', message: 'boom' });
    expect(callImpl).toHaveBeenCalledTimes(1);
  });
});
