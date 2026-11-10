/**
 * memory-entries.ts — Story 16.1 — REST routes for River Memory CRUD
 *
 * GET    /api/memory-entries              → list (optional ?scope=user|project|session)
 * POST   /api/memory-entries              → create → 201
 * GET    /api/memory-entries/settings     → read enabled flag
 * PUT    /api/memory-entries/settings     → update enabled flag
 * PATCH  /api/memory-entries/:id          → partial update
 * DELETE /api/memory-entries/:id          → 204
 *
 * IMPORTANT: /settings routes are registered BEFORE /:id so Express doesn't
 * treat the literal string "settings" as an id parameter.
 */

import { Router, Request, Response } from 'express';
import {
  listEntries,
  createEntry,
  updateEntry,
  deleteEntry,
  getSettings,
  updateSettings,
  isValidId,
} from '../storage/memory-entries';
import type { MemoryScope } from '../storage/memory-entries';

const router = Router();

// ── Collection ────────────────────────────────────────────────────────────────

router.get('/', (req: Request, res: Response) => {
  const { scope } = req.query;
  const validScopes: MemoryScope[] = ['user', 'project', 'session'];
  const scopeFilter =
    typeof scope === 'string' && validScopes.includes(scope as MemoryScope)
      ? (scope as MemoryScope)
      : undefined;
  res.json(listEntries(scopeFilter));
});

router.post('/', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as { scope?: unknown; title?: unknown; content?: unknown };
  if (typeof body.scope !== 'string' || typeof body.title !== 'string') {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'scope and title are required' } });
    return;
  }
  try {
    const entry = createEntry({
      scope: body.scope as MemoryScope,
      title: body.title,
      content: typeof body.content === 'string' ? body.content : '',
    });
    res.status(201).json(entry);
  } catch (err) {
    const code = (err as Error).message;
    res.status(400).json({ error: { code, message: code } });
  }
});

// ── Settings — MUST be before /:id ────────────────────────────────────────────

router.get('/settings', (_req: Request, res: Response) => {
  res.json(getSettings());
});

router.put('/settings', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as { enabled?: unknown };
  if (typeof body.enabled !== 'boolean') {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'enabled (boolean) is required' } });
    return;
  }
  res.json(updateSettings({ enabled: body.enabled }));
});

// ── Single entry ──────────────────────────────────────────────────────────────

router.patch('/:id', (req: Request, res: Response) => {
  const { id } = req.params;
  if (!isValidId(id)) {
    res.status(400).json({ error: { code: 'INVALID_ID', message: 'id must be a valid UUID' } });
    return;
  }
  const body = (req.body ?? {}) as { scope?: unknown; title?: unknown; content?: unknown };
  const patch: Parameters<typeof updateEntry>[1] = {};
  if (body.scope !== undefined) patch.scope = body.scope as MemoryScope;
  if (body.title !== undefined) patch.title = body.title as string;
  if (body.content !== undefined) patch.content = body.content as string;

  try {
    const updated = updateEntry(id, patch);
    if (!updated) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Entry not found' } });
      return;
    }
    res.json(updated);
  } catch (err) {
    const code = (err as Error).message;
    res.status(400).json({ error: { code, message: code } });
  }
});

router.delete('/:id', (req: Request, res: Response) => {
  const { id } = req.params;
  if (!isValidId(id)) {
    res.status(400).json({ error: { code: 'INVALID_ID', message: 'id must be a valid UUID' } });
    return;
  }
  const ok = deleteEntry(id);
  if (!ok) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Entry not found' } });
    return;
  }
  res.status(204).send();
});

export default router;
