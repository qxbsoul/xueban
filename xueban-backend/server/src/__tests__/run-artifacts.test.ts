// server/src/__tests__/run-artifacts.test.ts
// 多产物登记（2026-11-07）— run_artifacts 独立产物表 + blueprint 捕获器。
//
// 动机：run-sessions 此前「last blueprint wins」（runs 表三字段只留最后一件
// 产物），多产物 run 的其余产物在产物面板/项目产物里全部不可见。
//
// 覆盖四层：
//   1. captureBlueprintArtifact 纯逻辑（多件登记/同名覆盖去重/空载荷跳过/主产物语义）
//   2. storage/runs.ts run_artifacts 读写（幂等重登记/捕获序/deleteRun 级联/孤儿清理）
//   3. GET /api/projects/:id/artifacts 多产物逐件展开 + 旧 run 三字段回落
//   4. GET /api/runs 行内 artifacts 数组（additive，raw array 契约不破）
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { captureBlueprintArtifact, type CapturedArtifact } from '../routes/run-sessions';

let tmp: string;
let origCwd: string;
let sqlite: typeof import('../storage/sqlite');
let runsStore: typeof import('../storage/runs');
let projectsStore: typeof import('../storage/projects');
let projectsRouter: typeof import('../routes/projects').default;
let runsRouter: typeof import('../routes/runs').default;

beforeAll(async () => {
  // sqlite 数据文件落在 cwd/.xueban/ — 切到临时目录跑隔离库。
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-run-artifacts-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  runsStore = await import('../storage/runs');
  projectsStore = await import('../storage/projects');
  projectsRouter = (await import('../routes/projects')).default;
  runsRouter = (await import('../routes/runs')).default;
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

function makeRecord(overrides: Partial<import('../storage/runs').RunRecord> = {}) {
  return {
    run_id: 'run-art1',
    session_id: 'sess-art1',
    goal: '生成团队蓝图与原型',
    skill_name: 'agent-team-blueprint',
    skill_display_name: 'Agent Team Blueprint',
    artifact_type: 'yaml' as const,
    artifact_filename: 'team_blueprint.yml',
    artifact_url: '/projects/sess-art1/team_blueprint.yml',
    status: 'completed' as const,
    created_at: '2026-11-07T02:00:00Z',
    completed_at: '2026-11-07T02:00:30Z',
    project_dir: '.xueban/projects/sess-art1',
    ...overrides,
  };
}

// ── 1. captureBlueprintArtifact 纯逻辑 ──────────────────────────────────────

describe('captureBlueprintArtifact（blueprint 事件捕获器）', () => {
  it('多件产物逐件登记（不再 last-wins 丢失）', () => {
    const captured: CapturedArtifact[] = [];
    const e1 = captureBlueprintArtifact(captured, {
      artifact_type: 'yaml',
      filename: 'team_blueprint.yml',
      artifact_url: '/projects/s1/team_blueprint.yml',
    });
    const e2 = captureBlueprintArtifact(captured, {
      artifact_type: 'html',
      filename: 'prototype.html',
      artifact_url: '/projects/s1/prototype.html',
    });
    const e3 = captureBlueprintArtifact(captured, {
      artifact_type: 'markdown',
      filename: 'report.md',
    });
    expect(captured).toHaveLength(3);
    expect(captured.map((a) => a.filename)).toEqual([
      'team_blueprint.yml',
      'prototype.html',
      'report.md',
    ]);
    // 返回值 = 本次条目：调用方用最后一次维持「主产物」旧语义。
    expect(e1?.type).toBe('yaml');
    expect(e2?.type).toBe('html');
    expect(e3?.url).toBeNull();
  });

  it('同名产物重复事件 → 覆盖既有行（后值补齐缺字段），不堆行', () => {
    const captured: CapturedArtifact[] = [];
    captureBlueprintArtifact(captured, { filename: 'a.html' });
    captureBlueprintArtifact(captured, {
      artifact_type: 'html',
      filename: 'a.html',
      artifact_url: '/projects/s1/a.html',
    });
    expect(captured).toHaveLength(1);
    expect(captured[0]).toEqual({
      type: 'html',
      filename: 'a.html',
      url: '/projects/s1/a.html',
    });
  });

  it('空载荷（type/filename/url 全无）→ 不登记，返回 null', () => {
    const captured: CapturedArtifact[] = [];
    expect(captureBlueprintArtifact(captured, {})).toBeNull();
    expect(captureBlueprintArtifact(captured, { filename: '', artifact_url: '' })).toBeNull();
    expect(captured).toHaveLength(0);
  });

  it('非法 artifact_type 落 null 但产物仍登记（filename 为准）', () => {
    const captured: CapturedArtifact[] = [];
    const e = captureBlueprintArtifact(captured, {
      artifact_type: 'exe/unknown',
      filename: 'x.bin',
    });
    expect(e?.type).toBeNull();
    expect(captured).toHaveLength(1);
  });
});

// ── 2. storage/runs.ts run_artifacts 读写 ───────────────────────────────────

describe('run_artifacts 存储', () => {
  it('saveRunArtifacts/listRunArtifacts 按捕获序往返；重复登记幂等', () => {
    runsStore.saveRun(makeRecord());
    runsStore.saveRunArtifacts('run-art1', [
      { artifact_type: 'yaml', artifact_filename: 'a.yml', artifact_url: '/p/a.yml' },
      { artifact_type: 'html', artifact_filename: 'b.html', artifact_url: '/p/b.html' },
      { artifact_type: 'markdown', artifact_filename: 'c.md', artifact_url: null },
    ]);
    expect(runsStore.listRunArtifacts('run-art1')).toEqual([
      { artifact_type: 'yaml', artifact_filename: 'a.yml', artifact_url: '/p/a.yml' },
      { artifact_type: 'html', artifact_filename: 'b.html', artifact_url: '/p/b.html' },
      { artifact_type: 'markdown', artifact_filename: 'c.md', artifact_url: null },
    ]);
    // 幂等：回调触发两次/重放 → 全量重登记，不堆行。
    runsStore.saveRunArtifacts('run-art1', [
      { artifact_type: 'yaml', artifact_filename: 'a.yml', artifact_url: '/p/a.yml' },
    ]);
    expect(runsStore.listRunArtifacts('run-art1')).toHaveLength(1);
  });

  it('deleteRun 级联清掉登记行', () => {
    runsStore.saveRun(makeRecord({ run_id: 'run-del', session_id: 'sess-del' }));
    runsStore.saveRunArtifacts('run-del', [
      { artifact_type: 'yaml', artifact_filename: 'd.yml', artifact_url: null },
    ]);
    expect(runsStore.deleteRun('run-del')).toBe(true);
    expect(runsStore.listRunArtifacts('run-del')).toHaveLength(0);
  });

  it('saveRun 落库时清掉被 trim 删掉的 run 的孤儿登记行', () => {
    runsStore.saveRun(makeRecord({ run_id: 'run-orphan', session_id: 'sess-orphan' }));
    runsStore.saveRunArtifacts('run-orphan', [
      { artifact_type: 'yaml', artifact_filename: 'o.yml', artifact_url: null },
    ]);
    // 模拟 trim：run 行被裁掉但登记行还在（run_artifacts 不挂 FK）。
    sqlite.getDb().prepare(`DELETE FROM runs WHERE run_id = ?`).run('run-orphan');
    // 任意一次 saveRun 落库都会顺手清孤儿。
    runsStore.saveRun(makeRecord({ run_id: 'run-trim-trigger', session_id: 'sess-tt' }));
    expect(runsStore.listRunArtifacts('run-orphan')).toHaveLength(0);
  });

  it('listAllRunArtifacts 按 run 分组', () => {
    runsStore.saveRun(makeRecord({ run_id: 'run-g1', session_id: 'sess-g1' }));
    runsStore.saveRun(makeRecord({ run_id: 'run-g2', session_id: 'sess-g2' }));
    runsStore.saveRunArtifacts('run-g1', [
      { artifact_type: 'yaml', artifact_filename: 'g1.yml', artifact_url: null },
      { artifact_type: 'html', artifact_filename: 'g1.html', artifact_url: null },
    ]);
    runsStore.saveRunArtifacts('run-g2', [
      { artifact_type: 'markdown', artifact_filename: 'g2.md', artifact_url: null },
    ]);
    const byRun = runsStore.listAllRunArtifacts();
    expect(byRun.get('run-g1')?.map((a) => a.artifact_filename)).toEqual(['g1.yml', 'g1.html']);
    expect(byRun.get('run-g2')?.map((a) => a.artifact_filename)).toEqual(['g2.md']);
  });
});

// ── 3. GET /api/projects/:id/artifacts ──────────────────────────────────────

function appWith(routers: Array<{ base: string; router: express.Router }>) {
  const a = express();
  a.use(express.json());
  for (const r of routers) a.use(r.base, r.router);
  return a;
}

describe('GET /api/projects/:id/artifacts（多产物逐件展开）', () => {
  it('新式 run：登记 3 件 → 3 行（artifact_id=run#seq），不再只显示最后一件', async () => {
    const p = projectsStore.createProject({ name: 'multi', workspace_path: path.join(tmp, 'pm') });
    runsStore.saveRun(
      makeRecord({ run_id: 'run-m1', session_id: 'sess-m1' }),
      { project_id: p.project_id },
    );
    runsStore.saveRunArtifacts('run-m1', [
      { artifact_type: 'yaml', artifact_filename: 'a.yml', artifact_url: '/projects/sess-m1/a.yml' },
      { artifact_type: 'html', artifact_filename: 'b.html', artifact_url: '/projects/sess-m1/b.html' },
      { artifact_type: 'markdown', artifact_filename: 'c.md', artifact_url: null },
    ]);
    const res = await request(appWith([{ base: '/api/projects', router: projectsRouter }]))
      .get(`/api/projects/${p.project_id}/artifacts`);
    expect(res.status).toBe(200);
    const rows = res.body as Array<{
      artifact_id: string;
      title: string;
      file_type: string;
      download_url: string | null;
      preview_url: string | null;
      run_id: string;
    }>;
    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.artifact_id)).toEqual(['run-m1#0', 'run-m1#1', 'run-m1#2']);
    expect(rows.map((r) => r.title)).toEqual(['a.yml', 'b.html', 'c.md']);
    expect(rows.every((r) => r.run_id === 'run-m1')).toBe(true);
    // html → preview_url 有值；markdown → null（口径与旧行为一致）。
    expect(rows[1].file_type).toBe('html');
    expect(rows[1].preview_url).toBe('/projects/sess-m1/b.html');
    expect(rows[2].file_type).toBe('md');
    expect(rows[2].preview_url).toBeNull();
  });

  it('旧式 run（无登记行）→ 回落 runs 三字段单行，artifact_id 保持 run_id', async () => {
    const p = projectsStore.createProject({ name: 'legacy', workspace_path: path.join(tmp, 'pl') });
    runsStore.saveRun(
      makeRecord({ run_id: 'run-l1', session_id: 'sess-l1' }),
      { project_id: p.project_id },
    );
    const res = await request(appWith([{ base: '/api/projects', router: projectsRouter }]))
      .get(`/api/projects/${p.project_id}/artifacts`);
    expect(res.status).toBe(200);
    const rows = res.body as Array<{ artifact_id: string; title: string; run_id: string }>;
    expect(rows).toHaveLength(1);
    expect(rows[0].artifact_id).toBe('run-l1');
    expect(rows[0].title).toBe('team_blueprint.yml');
    expect(rows[0].run_id).toBe('run-l1');
  });

  it('无产物 run 不出行；登记空 + 三字段为空也不出行', async () => {
    const p = projectsStore.createProject({ name: 'empty', workspace_path: path.join(tmp, 'pe') });
    runsStore.saveRun(
      makeRecord({
        run_id: 'run-e1',
        session_id: 'sess-e1',
        artifact_type: null,
        artifact_filename: null,
        artifact_url: null,
      }),
      { project_id: p.project_id },
    );
    runsStore.saveRunArtifacts('run-e1', []);
    const res = await request(appWith([{ base: '/api/projects', router: projectsRouter }]))
      .get(`/api/projects/${p.project_id}/artifacts`);
    expect(res.status).toBe(200);
    expect(res.body).toEqual([]);
  });
});

// ── 4. GET /api/runs（行内 artifacts，additive）──────────────────────────────

describe('GET /api/runs 行内 artifacts 数组', () => {
  it('登记全集内嵌；旧 run 给空数组；raw array 契约保持', async () => {
    // 真实链路：runs 三字段 = 主产物（捕获的最后一件 r1.html），全集在 artifacts。
    runsStore.saveRun(
      makeRecord({
        run_id: 'run-r1',
        session_id: 'sess-r1',
        artifact_type: 'html',
        artifact_filename: 'r1.html',
        artifact_url: '/p/r1.html',
      }),
    );
    runsStore.saveRunArtifacts('run-r1', [
      { artifact_type: 'yaml', artifact_filename: 'r1.yml', artifact_url: '/p/r1.yml' },
      { artifact_type: 'html', artifact_filename: 'r1.html', artifact_url: '/p/r1.html' },
    ]);
    runsStore.saveRun(makeRecord({ run_id: 'run-r2', session_id: 'sess-r2' }));

    const res = await request(appWith([{ base: '/api/runs', router: runsRouter }])).get('/api/runs');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    const rows = res.body as Array<{
      run_id: string;
      artifact_filename: string | null;
      artifacts: Array<{ artifact_type: string | null; artifact_filename: string | null; artifact_url: string | null }>;
    }>;
    const r1 = rows.find((r) => r.run_id === 'run-r1');
    const r2 = rows.find((r) => r.run_id === 'run-r2');
    expect(r1?.artifacts.map((a) => a.artifact_filename)).toEqual(['r1.yml', 'r1.html']);
    // 主产物三字段仍是最后一件（旧行为），全集在 artifacts 里。
    expect(r1?.artifact_filename).toBe('r1.html');
    expect(r2?.artifacts).toEqual([]);
  });
});
