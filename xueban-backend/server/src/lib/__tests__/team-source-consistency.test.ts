import { describe, it, expect, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { loadTeamForRun } from '../team-source';

describe('真源一致性 — 写后读 edges/policy 不偏差', () => {
  const tmpDirs: string[] = [];
  const mkTmp = () => { const d = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-teams-')); tmpDirs.push(d); return d; };
  afterAll(() => { for (const d of tmpDirs) { try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* ignore */ } } });

  it('putTeamWorkflow 落库形态 → loadTeamForRun 还原的 edges/policy 与团队页一致', () => {
    const dir = mkTmp();
    // 这份 JSON = RunSession buildWorkflow + putTeamPolicy 写进 Python 的真实结构
    const saved = {
      team_id: 'tc', name: 'BMAD', agent_ids: ['pm', 'arch', 'dev', 'qa'],
      workflow: {
        nodes: ['pm', 'arch', 'dev', 'qa'].map((a, i) => ({ id: `n-${a}`, position: { x: i * 200, y: 0 }, data: { agentId: a } })),
        edges: [
          { id: 'e1', source: 'n-pm', target: 'n-arch', data: { mode: 'direct' } },
          { id: 'e2', source: 'n-arch', target: 'n-dev', data: { mode: 'direct' } },
          { id: 'e3', source: 'n-dev', target: 'n-qa', data: { mode: 'direct' } },
          { id: 'e4', source: 'n-qa', target: 'n-dev', data: { mode: 'conditional' } }, // 回归边
        ],
      },
      policy_matrix: { pm: { arch: 'permit' }, qa: { dev: 'warn' } },
    };
    fs.writeFileSync(path.join(dir, 'tc.json'), JSON.stringify(saved));

    const { team, errors } = loadTeamForRun('tc', [dir]);
    expect(errors).toEqual([]);
    expect(team?.members).toEqual(['pm', 'arch', 'dev', 'qa']);
    // edges 按 Python JSON workflow.edges 的数组顺序保留(回归边 qa→dev 在末位)
    expect(team?.edges).toEqual([
      { from: 'pm', to: 'arch', kind: 'sequential' },
      { from: 'arch', to: 'dev', kind: 'sequential' },
      { from: 'dev', to: 'qa', kind: 'sequential' },
      { from: 'qa', to: 'dev', kind: 'conditional' }, // 回归边保真
    ]);
    expect(team?.policy_matrix).toEqual({ pm: { arch: 'permit' }, qa: { dev: 'warn' } });
  });
});
