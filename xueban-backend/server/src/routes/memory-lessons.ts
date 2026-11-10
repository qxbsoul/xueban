/**
 * memory-lessons.ts — 经验记忆路由（2026-11-05，移植自 XUEBAN-OS PR#3 memory.py 设计）。
 *
 * GET    /api/memory/lessons          → 列表（score 降序；?scope_key=&kind=&includeArchived=&limit=）
 * POST   /api/memory/lessons          → 记一条（重复内容自动加权而非新增）→ 201
 * GET    /api/memory/lessons/stats    → 统计（total/by_kind/top_k/max_per_scope）
 * DELETE /api/memory/lessons/:id      → 软删（归档留痕）→ 200
 *
 * IMPORTANT: /stats 注册在 /:id 之前，避免 Express 把 "stats" 当 id。
 * 与手动 River Memory（/api/memory-entries）并存双区、互不覆盖。
 */

import { Router, Request, Response } from 'express';
import {
  listLessons,
  lessonStats,
  forgetLesson,
  remember,
  MemoryLessonError,
  KIND_LESSON,
  KIND_PREF,
  KIND_WEAK,
} from '../storage/memory-lessons';

const router = Router();

const VALID_KINDS = [KIND_LESSON, KIND_PREF, KIND_WEAK];

// ── Collection ────────────────────────────────────────────────────────────────

router.get('/', (req: Request, res: Response) => {
  const { scope_key, kind, includeArchived, limit } = req.query;
  const rows = listLessons({
    scope_key: typeof scope_key === 'string' ? scope_key : undefined,
    kind: typeof kind === 'string' && VALID_KINDS.includes(kind) ? kind : undefined,
    includeArchived: includeArchived === '1' || includeArchived === 'true',
    limit: typeof limit === 'string' ? Number(limit) : undefined,
  });
  res.json(rows);
});

router.post('/', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  if (typeof body.content !== 'string' || !body.content.trim()) {
    res
      .status(400)
      .json({ error: { code: 'INVALID_BODY', message: 'content (non-empty string) is required' } });
    return;
  }
  const kind = typeof body.kind === 'string' && VALID_KINDS.includes(body.kind) ? body.kind : KIND_LESSON;
  try {
    const entry = remember({
      content: body.content,
      kind,
      scope_key: typeof body.scope_key === 'string' ? body.scope_key : '',
      task_type: typeof body.task_type === 'string' ? body.task_type : '',
      source: typeof body.source === 'string' && body.source ? body.source : 'manual',
      source_ref: typeof body.source_ref === 'string' ? body.source_ref : '',
      title: typeof body.title === 'string' ? body.title : '',
      score: typeof body.score === 'number' ? body.score : undefined,
    });
    res.status(201).json(entry);
  } catch (err) {
    if (err instanceof MemoryLessonError) {
      res.status(err.status).json({ error: { code: 'MEMORY_ERROR', message: err.message } });
      return;
    }
    res.status(500).json({ error: { code: 'INTERNAL', message: (err as Error).message } });
  }
});

// ── Stats — MUST be before /:id ───────────────────────────────────────────────

router.get('/stats', (_req: Request, res: Response) => {
  res.json(lessonStats());
});

// ── Single entry ──────────────────────────────────────────────────────────────

router.delete('/:id', (req: Request, res: Response) => {
  try {
    const removed = forgetLesson(req.params.id);
    res.json(removed);
  } catch (err) {
    if (err instanceof MemoryLessonError && err.status === 404) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Lesson not found' } });
      return;
    }
    res.status(500).json({ error: { code: 'INTERNAL', message: (err as Error).message } });
  }
});

export default router;
