import { describe, it, expect } from 'vitest';
import { computeDagLayers } from '../dag-layers';
import type { TeamEdgeV1 } from '../../team-yaml';

const E = (from: string, to: string, kind: TeamEdgeV1['kind'] = 'sequential'): TeamEdgeV1 => ({ from, to, kind });

describe('computeDagLayers', () => {
  it('线性链 PM→Arch→Dev→QA → 4 层各一人', () => {
    const r = computeDagLayers(['pm', 'arch', 'dev', 'qa'], [E('pm', 'arch'), E('arch', 'dev'), E('dev', 'qa')]);
    expect(r.layers).toEqual([['pm'], ['arch'], ['dev'], ['qa']]);
  });

  it('无边 → 全部入第 0 层(并行扇出)', () => {
    const r = computeDagLayers(['a', 'b', 'c'], []);
    expect(r.layers).toEqual([['a', 'b', 'c']]);
  });

  it('菱形:a→b,a→c,b→d,c→d → 层 [[a],[b,c],[d]](同层并行)', () => {
    const r = computeDagLayers(['a', 'b', 'c', 'd'], [E('a', 'b'), E('a', 'c'), E('b', 'd'), E('c', 'd')]);
    expect(r.layers).toEqual([['a'], ['b', 'c'], ['d']]);
  });

  it('conditional 回归边被排除出拓扑、计入 ignoredEdges', () => {
    const edges = [E('pm', 'arch'), E('arch', 'dev'), E('dev', 'qa'), E('qa', 'dev', 'conditional')];
    const r = computeDagLayers(['pm', 'arch', 'dev', 'qa'], edges);
    expect(r.layers).toEqual([['pm'], ['arch'], ['dev'], ['qa']]);
    expect(r.ignoredEdges).toEqual([{ from: 'qa', to: 'dev', kind: 'conditional' }]);
  });

  it('引用非成员的边被丢弃,不产生幽灵节点', () => {
    const r = computeDagLayers(['a', 'b'], [E('a', 'b'), E('a', 'ghost'), E('x', 'b')]);
    expect(r.layers).toEqual([['a'], ['b']]);
    expect(r.ignoredEdges).toEqual([]);
    expect(r.cyclicNodes).toEqual([]);
  });

  it('纯 sequential 成环(非 conditional)→ 环节点兜底进末层 + cyclicNodes 记录', () => {
    const r = computeDagLayers(['a', 'b'], [E('a', 'b'), E('b', 'a')]);
    expect([...r.cyclicNodes].sort()).toEqual(['a', 'b']);
    expect([...r.layers[r.layers.length - 1]].sort()).toEqual(['a', 'b']);
  });

  it('成员顺序在同层内稳定(按 members 输入序)', () => {
    const r = computeDagLayers(['c', 'a', 'b'], []);
    expect(r.layers).toEqual([['c', 'a', 'b']]);
  });

  it('自环边 a→a 被丢弃,a 正常入第 0 层、不进 cyclicNodes', () => {
    const r = computeDagLayers(['a', 'b'], [E('a', 'a'), E('a', 'b')]);
    expect(r.layers).toEqual([['a'], ['b']]);
    expect(r.cyclicNodes).toEqual([]);
  });
});
