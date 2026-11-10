/**
 * runs.ts — GET /api/runs (list) + DELETE /api/runs/:runId
 *
 * Projection endpoints (/runs/:runId/graph, /task-tree) are served locally for
 * Node-daemon runs (see L6 block below) and proxied to Python FastAPI
 * (xueban/api/runs.py, RuntimeService export_*) for Python-runtime runs.
 *
 * Contract (2026-05-10 review B2 — 守 Story 15.1 决议)：返回 raw array `RunRecord[]`，
 * 与前端 `listRuns(): Promise<RunRecord[]>` (src/api/runs.ts:295) 直接对齐。
 *
 * 15.8 dev 早期把响应改成 `{runs, total}` envelope，违反了 15.1 review 的明确决议
 * （runs 走 raw array，agents 走 envelope，skills 走 raw array）。已回退。
 *
 * 历史：15.1 placeholder `[]` → 15.8 envelope（错误）→ 此次修正回到 raw array。
 */

import { Router, Request, Response, NextFunction } from 'express';
import { listRuns, listAllRunArtifacts, deleteRun } from '../storage/runs';
import { getRunReplay } from './run-sessions';
import {
  buildDaemonRunGraph,
  buildDaemonTaskTree,
} from '../lib/run-projection';

const router = Router();

// GET /runs → RunRecord[] (raw array, NOT envelope — see contract note above)
// ?limit= (frontend src/api/runs.ts sends it): parse int, clamp 1..200;
// absent/invalid → full list (previous behavior, table is already MAX_RUNS-capped).
//
// 多产物登记（2026-11-07）— 行内附加 `artifacts` 数组（additive）：该 run
// 登记的全部产物按捕获序；无登记行的旧 run 给空数组（消费侧回落
// artifact_* 三字段主产物，形状不破坏 15.1 raw array 契约）。
router.get('/', (req: Request, res: Response) => {
  const runs = listRuns();
  const artifactsByRun = listAllRunArtifacts();
  const withArtifacts = runs.map((r) => ({
    ...r,
    artifacts: artifactsByRun.get(r.run_id) ?? [],
  }));
  const raw = req.query.limit;
  if (typeof raw === 'string' && raw.trim() !== '') {
    const n = parseInt(raw, 10);
    if (!Number.isNaN(n)) {
      const limit = Math.min(Math.max(n, 1), 200);
      res.json(withArtifacts.slice(0, limit));
      return;
    }
  }
  res.json(withArtifacts);
});

// ── L6（GAP.md:13）— Node daemon run 的投影端点 ─────────────────────────────
// chat 主路径（Node daemon）启动的 run 在 Python RuntimeService 无记录，投影
// 请求经 proxyFallback 打到 Python 只能 404，前端「过程」tab 显示错误条。
// 这里先在 Node 侧查 daemon 事件日志（run-sessions.getRunReplay，含磁盘
// hydrate），命中则由 lib/run-projection 折叠出与 Python 相同形状的投影；
// 未命中（Python runtime 的 run）→ next() 落回 proxyFallback，行为零变化。
router.get('/:runId/graph', (req: Request, res: Response, next: NextFunction) => {
  const replay = getRunReplay(req.params.runId);
  if (!replay) {
    next();
    return;
  }
  res.json(buildDaemonRunGraph(req.params.runId, replay));
});

router.get('/:runId/task-tree', (req: Request, res: Response, next: NextFunction) => {
  const replay = getRunReplay(req.params.runId);
  if (!replay) {
    next();
    return;
  }
  res.json(buildDaemonTaskTree(req.params.runId, replay));
});

// DELETE /runs/:runId
router.delete('/:runId', (req: Request, res: Response) => {
  const deleted = deleteRun(req.params.runId);
  if (deleted) res.json({ ok: true });
  else res.status(404).json({ error: `Run ${req.params.runId} not found` });
});

// Remaining projection endpoints are still proxied to Python (daemon runs have
// no lineage/memory/checkpoint/training data server-side):
// - GET /runs/:runId/artifact-lineage → xueban/api/runs.py:get_artifact_lineage
// - GET /runs/:runId/memory-graph → xueban/api/runs.py:get_memory_graph
// - GET /runs/:runId/checkpoint-lineage → xueban/api/runs.py:get_checkpoint_lineage
// - GET /runs/:runId/training-dataset → xueban/api/runs.py:get_training_dataset
//
// The proxy-fallback middleware handles these by forwarding to the Python backend.

export default router;
