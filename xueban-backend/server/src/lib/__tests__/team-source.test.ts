import { describe, it, expect, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { mapPythonTeamToRunShape, loadTeamForRun } from '../team-source';

// Python JSON 真源样例(teams.py 存的形态:agent_ids + workflow{nodes,edges} + policy_matrix)
const pyTeam = {
  team_id: 't1',
  name: 'BMAD 团队',
  agent_ids: ['a1', 'a2', 'a3'],
  workflow: {
    nodes: [
      { id: 'n1', type: 'agentTask', position: { x: 0, y: 0 }, data: { agentId: 'a1', name: 'PM' } },
      { id: 'n2', type: 'agentTask', position: { x: 200, y: 0 }, data: { agentId: 'a2', name: 'Arch' } },
      { id: 'n3', type: 'agentTask', position: { x: 400, y: 0 }, data: { agentId: 'a3', name: 'Dev' } },
    ],
    edges: [
      { id: 'e1', source: 'n1', target: 'n2', data: { mode: 'direct' } },
      { id: 'e2', source: 'n2', target: 'n3', data: { mode: 'conditional' } },
    ],
  },
  policy_matrix: { a1: { a2: 'permit' }, a2: { a3: 'warn' } },
};

describe('mapPythonTeamToRunShape', () => {
  it('members = agent_ids', () => {
    expect(mapPythonTeamToRunShape(pyTeam).members).toEqual(['a1', 'a2', 'a3']);
  });
  it('workflow.edges(source/target)→ edges_v1(from/to),node id 经 nodeId→agentId 解析', () => {
    const { edges } = mapPythonTeamToRunShape(pyTeam);
    expect(edges).toContainEqual({ from: 'a1', to: 'a2', kind: 'sequential' });
    expect(edges).toContainEqual({ from: 'a2', to: 'a3', kind: 'conditional' });
  });
  it('policy_matrix 原样透传', () => {
    expect(mapPythonTeamToRunShape(pyTeam).policy_matrix).toEqual({ a1: { a2: 'permit' }, a2: { a3: 'warn' } });
  });
  it('空 workflow / 缺字段不崩,返回空 members/edges', () => {
    const r = mapPythonTeamToRunShape({ team_id: 't0' });
    expect(r.members).toEqual([]);
    expect(r.edges).toEqual([]);
    expect(r.policy_matrix).toEqual({});
  });
  it('edge 引用未知 node → 跳过该边,不产生悬空边', () => {
    const r = mapPythonTeamToRunShape({
      team_id: 't2', agent_ids: ['a1'],
      workflow: { nodes: [{ id: 'n1', data: { agentId: 'a1' } }], edges: [{ source: 'n1', target: 'nX' }] },
    });
    expect(r.edges).toEqual([]);
  });
  it('workflow.edge.data.max_retries 透传到 TeamEdgeV1', () => {
    const r = mapPythonTeamToRunShape({
      team_id: 't', agent_ids: ['a', 'b'],
      workflow: { nodes: [{ id: 'n1', data: { agentId: 'a' } }, { id: 'n2', data: { agentId: 'b' } }],
                  edges: [{ source: 'n1', target: 'n2', data: { mode: 'direct', max_retries: 5 } }] },
    });
    expect(r.edges[0]).toEqual({ from: 'a', to: 'b', kind: 'sequential', max_retries: 5 });
  });
  it('node 缺 agentId → 该节点不入 map,指向它的边视为悬空跳过', () => {
    const r = mapPythonTeamToRunShape({
      team_id: 't3',
      workflow: {
        nodes: [{ id: 'n1', data: {} }],
        edges: [{ source: 'n1', target: 'n1' }],
      },
    });
    expect(r.edges).toEqual([]);
  });

  // 2026-11-05 历史教训回归防线：mapMode 曾把 mode:'approve' 折叠成
  // 'sequential'，审批语义在读路径就丢了（chat 执行器无从知道该边要人工
  // 审批）。此表钉死 mapMode 全量契约，防复发。
  it.each([
    ['approve', 'approve'],
    ['parallel', 'parallel'],
    ['conditional', 'conditional'],
    ['direct', 'sequential'],
    ['something-new', 'sequential'],
  ] as const)('mapMode 契约:mode:%s → kind:%s（防折叠回归）', (mode, kind) => {
    const r = mapPythonTeamToRunShape({
      team_id: 't', agent_ids: ['a', 'b'],
      workflow: {
        nodes: [{ id: 'n1', data: { agentId: 'a' } }, { id: 'n2', data: { agentId: 'b' } }],
        edges: [{ source: 'n1', target: 'n2', data: { mode } }],
      },
    });
    expect(r.edges[0]).toEqual({ from: 'a', to: 'b', kind });
  });
  it('edge.data 缺 mode → sequential（缺省不丢审批语义前提:只有显式 approve 才是审批边）', () => {
    const r = mapPythonTeamToRunShape({
      team_id: 't', agent_ids: ['a', 'b'],
      workflow: {
        nodes: [{ id: 'n1', data: { agentId: 'a' } }, { id: 'n2', data: { agentId: 'b' } }],
        edges: [{ source: 'n1', target: 'n2' }],
      },
    });
    expect(r.edges[0]).toEqual({ from: 'a', to: 'b', kind: 'sequential' });
  });
});

describe('loadTeamForRun', () => {
  const tmpDirs: string[] = [];
  const mkTmp = () => { const d = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-teams-')); tmpDirs.push(d); return d; };
  afterAll(() => { for (const d of tmpDirs) { try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* ignore */ } } });

  it('读到 <id>.json → 返回映射后的 run shape', () => {
    const dir = mkTmp();
    fs.writeFileSync(path.join(dir, 't1.json'), JSON.stringify({
      team_id: 't1', agent_ids: ['a1', 'a2'],
      workflow: { nodes: [{ id: 'n1', data: { agentId: 'a1' } }, { id: 'n2', data: { agentId: 'a2' } }],
                  edges: [{ source: 'n1', target: 'n2', data: { mode: 'direct' } }] },
      policy_matrix: { a1: { a2: 'permit' } },
    }));
    const r = loadTeamForRun('t1', [dir]);
    expect(r.team?.members).toEqual(['a1', 'a2']);
    expect(r.team?.edges).toEqual([{ from: 'a1', to: 'a2', kind: 'sequential' }]);
    expect(r.errors).toEqual([]);
  });
  it('文件不存在 → team null + errors', () => {
    const dir = mkTmp();
    const r = loadTeamForRun('missing', [dir]);
    expect(r.team).toBeNull();
    expect(r.errors[0]).toMatch(/not found/i);
  });
  it('非法 id → 拒绝,不读盘', () => {
    const r = loadTeamForRun('../etc/passwd', ['/tmp']);
    expect(r.team).toBeNull();
    expect(r.errors[0]).toMatch(/invalid/i);
  });
  it('JSON 格式错误 → team null + parse failed error', () => {
    const dir = mkTmp();
    fs.writeFileSync(path.join(dir, 'bad.json'), '{ not json');
    const r = loadTeamForRun('bad', [dir]);
    expect(r.team).toBeNull();
    expect(r.errors[0]).toMatch(/parse failed/i);
  });
});
