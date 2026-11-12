/**
 * student-memory 路由（2026-11-12）。
 *
 * 所有操作都按登录账号隔离：owner_id 只来自 Bearer token，请求体里的 owner_id 不采信。
 */
import { Router, type Request, type Response } from 'express';
import { requireAccount } from '../lib/memory-account';
import {
  archiveMemory,
  correctMemory,
  getCandidate,
  getMemory,
  listCandidates,
  listJobs,
  listLearningEvents,
  listMemories,
  memoryEvidence,
  memoryVersions,
  rejectCandidate,
  acceptCandidate,
  MEMORY_CATEGORIES,
} from '../storage/student-memory';

const router = Router();

router.use(async (req: Request, res: Response, next) => {
  try {
    res.locals.ownerId = await requireAccount(req);
    next();
  } catch {
    res.status(401).json({ error: { code: 'UNAUTHORIZED', message: '请先登录后再访问学习记忆' } });
  }
});

function owner(res: Response): string {
  return String(res.locals.ownerId ?? '');
}

function fail(res: Response, error: unknown): void {
  console.error('[student-memory]', error);
  res.status(500).json({ error: { code: 'INTERNAL', message: '记忆服务暂时不可用，请稍后再试' } });
}

function str(value: unknown, max = 500): string {
  return typeof value === 'string' ? value.slice(0, max) : '';
}

router.get('/memories', (req: Request, res: Response) => {
  const category = str(req.query.category, 32);
  res.json(listMemories({
    owner_id: owner(res),
    subject: str(req.query.subject, 20) || undefined,
    category: MEMORY_CATEGORIES.includes(category as never) ? category : undefined,
    status: str(req.query.status, 20) || 'active',
    limit: Number(req.query.limit) || 100,
  }));
});

router.get('/memories/:id', (req: Request, res: Response) => {
  const memory = getMemory(owner(res), req.params.id);
  if (!memory) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '记忆不存在' } });
    return;
  }
  res.json({
    memory,
    evidence: memoryEvidence(owner(res), memory.id),
    versions: memoryVersions(owner(res), memory.id).map((item) => ({
      ...item,
      snapshot: safeJson(item.snapshot_json),
    })),
  });
});

router.post('/memories/:id/correct', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  const content = str(body.content, 500).trim();
  if (!content) {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'content is required' } });
    return;
  }
  const memory = correctMemory(owner(res), req.params.id, {
    content,
    reason: str(body.reason, 300),
    confidence: typeof body.confidence === 'number' ? body.confidence : undefined,
  });
  if (!memory) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '记忆不存在' } });
    return;
  }
  res.json(memory);
});

router.post('/memories/:id/archive', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  if (!archiveMemory(owner(res), req.params.id, str(body.reason, 300))) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '记忆不存在' } });
    return;
  }
  res.status(204).end();
});

router.delete('/memories/:id', (req: Request, res: Response) => {
  if (!archiveMemory(owner(res), req.params.id, '学生删除')) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '记忆不存在' } });
    return;
  }
  res.status(204).end();
});

router.get('/candidates', (req: Request, res: Response) => {
  res.json(listCandidates(owner(res), str(req.query.status, 20) || 'pending', Number(req.query.limit) || 100));
});

router.post('/candidates/:id/accept', (req: Request, res: Response) => {
  try {
    const candidate = getCandidate(owner(res), req.params.id);
    if (!candidate) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: '候选记忆不存在' } });
      return;
    }
    res.json(acceptCandidate(owner(res), candidate.id, { changedBy: 'manual' }));
  } catch (error) {
    fail(res, error);
  }
});

router.post('/candidates/:id/reject', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as Record<string, unknown>;
  if (!rejectCandidate(owner(res), req.params.id, str(body.reason, 300))) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '候选记忆不存在或已处理' } });
    return;
  }
  res.status(204).end();
});

router.get('/events', (req: Request, res: Response) => {
  res.json(listLearningEvents(owner(res), Number(req.query.limit) || 50));
});

router.get('/jobs', (req: Request, res: Response) => {
  res.json(listJobs(owner(res), Number(req.query.limit) || 20));
});

function safeJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export default router;
