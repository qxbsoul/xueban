/**
 * projects.ts — Story 15.16 — REST API for the Project resource.
 *
 *   GET    /api/projects                    → list (newest first)
 *   GET    /api/projects?withSessions=1     → list + 每项附 sessions[]（项目分组会话，
 *                                             GAP A3 2026-08-22；session = conversations 行，
 *                                             group_id 互链 Python 群/DM，前端按它归组）
 *   POST   /api/projects                    → create  → 201
 *   GET    /api/projects/:id                → fetch   / 404
 *   PATCH  /api/projects/:id                → partial / 404
 *   DELETE /api/projects/:id                → 204     / 404
 *   GET    /api/projects/:id/artifacts      → artifact list from runs table
 *
 * Response shape mirrors the storage record verbatim — no envelope, no
 * camel/snake translation. Frontend can reflect the row directly.
 */

import { Router, Request, Response } from 'express';
import {
  createProject,
  deleteProject,
  getProject,
  listProjects,
  updateProject,
} from '../storage/projects';
import { listConversations } from '../storage/conversations';
import { getDb } from '../storage/sqlite';

const router = Router();

router.get('/', (req: Request, res: Response) => {
  const projects = listProjects();
  // GAP A3（2026-08-22）：?withSessions=1 → 每个项目附 sessions[]（该项目下的
  // 会话行，按 updated_at 新→旧）。数据模型复用 conversations.project_id（15.16
  // 已有外键），不新增表；shape 在 verbatim record 上额外加 id 别名 + sessions。
  if (req.query.withSessions === '1' || req.query.withSessions === 'true') {
    res.json(
      projects.map((p) => ({
        ...p,
        id: p.project_id,
        sessions: listConversations(p.project_id),
      })),
    );
    return;
  }
  res.json(projects);
});

router.post('/', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as {
    name?: unknown;
    workspace_path?: unknown;
    skill_id?: unknown;
    design_system_id?: unknown;
  };
  if (typeof body.name !== 'string' || body.name.trim().length === 0) {
    res
      .status(400)
      .json({ error: { code: 'INVALID_NAME', message: 'name is required' } });
    return;
  }
  const project = createProject({
    name: body.name.trim(),
    workspace_path:
      typeof body.workspace_path === 'string' && body.workspace_path.trim().length > 0
        ? body.workspace_path.trim()
        : undefined,
    skill_id: typeof body.skill_id === 'string' ? body.skill_id : null,
    design_system_id:
      typeof body.design_system_id === 'string' ? body.design_system_id : null,
  });
  res.status(201).json(project);
});

router.get('/:id', (req: Request, res: Response) => {
  const p = getProject(req.params.id);
  if (!p) {
    res
      .status(404)
      .json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
    return;
  }
  res.json(p);
});

router.patch('/:id', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  const patch: Parameters<typeof updateProject>[1] = {};
  if (typeof body.name === 'string') patch.name = body.name.trim();
  if (typeof body.workspace_path === 'string')
    patch.workspace_path = body.workspace_path.trim();
  if ('skill_id' in body)
    patch.skill_id = typeof body.skill_id === 'string' ? body.skill_id : null;
  if ('design_system_id' in body)
    patch.design_system_id =
      typeof body.design_system_id === 'string' ? body.design_system_id : null;

  const updated = updateProject(req.params.id, patch);
  if (!updated) {
    res
      .status(404)
      .json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
    return;
  }
  res.json(updated);
});

router.delete('/:id', (req: Request, res: Response) => {
  const ok = deleteProject(req.params.id);
  if (!ok) {
    res
      .status(404)
      .json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
    return;
  }
  res.status(204).send();
});

/**
 * GET /api/projects/:id/artifacts
 *
 * Returns every registered artifact of the project's runs.
 *
 * 多产物登记（2026-11-07）：一 run 多件产物时逐件返回一行（此前 runs 三
 * 字段「last blueprint wins」，一个 run 只能看到最后一件）。数据源两级：
 *   1. run_artifacts 独立登记表（新）→ 每件产物一行，artifact_id = run_id#seq；
 *   2. 无登记行的 run 回落 runs.artifact_* 三字段（旧数据兼容）→ 单行，
 *      artifact_id = run_id（保持旧值，消费侧 key/链接不断）。
 *
 * Response shape per item:
 *   artifact_id    — run_id#seq（多产物）或 run_id（旧单产物）
 *   title          — artifact_filename (basename) or goal as fallback
 *   file_type      — artifact_type ('html'|'md'|'yaml'|'pdf')
 *   generated_at   — completed_at
 *   download_url   — artifact_url (may be null)
 *   preview_url    — same as download_url for html; null otherwise
 *   size_bytes     — null (not stored yet)
 *   run_id         — run_id (for linking to run detail)
 */
router.get('/:id/artifacts', (req: Request, res: Response) => {
  const p = getProject(req.params.id);
  if (!p) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
    return;
  }

  interface ArtifactRow {
    run_id: string;
    goal: string;
    artifact_type: string | null;
    artifact_filename: string | null;
    artifact_url: string | null;
    completed_at: string;
  }

  interface RunArtifactRow {
    run_id: string;
    seq: number;
    artifact_type: string | null;
    artifact_filename: string | null;
    artifact_url: string | null;
  }

  const db = getDb();
  const runRows = db
    .prepare(
      `SELECT run_id, goal, artifact_type, artifact_filename, artifact_url, completed_at
       FROM runs
       WHERE project_id = ?
       ORDER BY completed_at DESC`,
    )
    .all(req.params.id) as ArtifactRow[];

  // run_artifacts 登记行（join runs 保证项目归属；seq ASC = 捕获序）。
  const artifactRows = db
    .prepare(
      `SELECT ra.run_id, ra.seq, ra.artifact_type, ra.artifact_filename, ra.artifact_url
       FROM run_artifacts ra
       JOIN runs r ON r.run_id = ra.run_id
       WHERE r.project_id = ?
       ORDER BY ra.seq ASC`,
    )
    .all(req.params.id) as RunArtifactRow[];
  const byRun = new Map<string, RunArtifactRow[]>();
  for (const a of artifactRows) {
    const list = byRun.get(a.run_id) ?? [];
    list.push(a);
    byRun.set(a.run_id, list);
  }

  const toRow = (
    run: ArtifactRow,
    artifactId: string,
    artifactType: string | null,
    filename: string | null,
    url: string | null,
  ) => {
    const ft = (artifactType ?? 'md').toLowerCase().replace('markdown', 'md');
    const isHtml = ft === 'html';
    return {
      artifact_id: artifactId,
      title: filename ?? run.goal,
      file_type: ft,
      generated_at: run.completed_at,
      download_url: url ?? null,
      preview_url: isHtml ? (url ?? null) : null,
      size_bytes: null,
      run_id: run.run_id,
    };
  };

  const artifacts = runRows.flatMap((r) => {
    const registered = byRun.get(r.run_id);
    if (registered && registered.length > 0) {
      // 新式 run：登记了什么就展示什么（含 runs 三字段之外的产物）。
      return registered.map((a) =>
        toRow(r, `${r.run_id}#${a.seq}`, a.artifact_type, a.artifact_filename, a.artifact_url),
      );
    }
    // 旧式 run：无登记行 → 回落 runs 三字段单件（保持旧行为/旧 artifact_id）。
    return r.artifact_filename ? [toRow(r, r.run_id, r.artifact_type, r.artifact_filename, r.artifact_url)] : [];
  });

  res.json(artifacts);
});

export default router;
