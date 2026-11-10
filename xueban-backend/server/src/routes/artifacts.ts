/**
 * routes/artifacts.ts — Story 15.14 — POST /api/artifacts/lint
 *
 * Body: { session_id: string, filename: string, type?: 'html'|'css'|'yaml'|'markdown' }
 *
 * Errors:
 *   400 INVALID_SESSION_ID   — session_id contains path-traversal chars or empty
 *   400 INVALID_FILENAME     — filename contains path-traversal chars / leading dot
 *   400 MISSING_FIELDS       — body missing session_id or filename
 *   404 ARTIFACT_NOT_FOUND   — file not under .xueban/projects/<session_id>/
 *   405 METHOD_NOT_ALLOWED   — non-POST verbs explicitly rejected
 *   500 LINT_INTERNAL_ERROR  — unexpected error inside lint dispatcher
 */

import { Router, Request, Response } from 'express';
import { runLint } from '../lint';

const router = Router();

router.post('/lint', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as { session_id?: unknown; filename?: unknown; type?: unknown };
  const session_id = typeof body.session_id === 'string' ? body.session_id : '';
  const filename = typeof body.filename === 'string' ? body.filename : '';
  const type = typeof body.type === 'string' ? body.type : undefined;

  if (!session_id || !filename) {
    res.status(400).json({
      error: { code: 'MISSING_FIELDS', message: 'session_id and filename are required' },
    });
    return;
  }

  try {
    const result = runLint(session_id, filename, type);
    res.status(200).json(result);
  } catch (err) {
    const code = (err as { code?: string }).code ?? '';
    if (code === 'INVALID_SESSION_ID') {
      res.status(400).json({
        error: {
          code,
          message: 'session_id must be alphanumeric (with - / _) and contain no path separators',
        },
      });
      return;
    }
    if (code === 'INVALID_FILENAME') {
      res.status(400).json({
        error: {
          code,
          message: 'filename must contain no path separators or .. and cannot start with a dot',
        },
      });
      return;
    }
    if (code === 'ARTIFACT_NOT_FOUND') {
      res.status(404).json({
        error: { code, message: `artifact ${filename} not found under session ${session_id}` },
      });
      return;
    }
    res.status(500).json({
      error: {
        code: 'LINT_INTERNAL_ERROR',
        message: err instanceof Error ? err.message : String(err),
      },
    });
  }
});

// 405 for non-POST verbs on /lint — explicit so old/buggy clients see something
// useful rather than a 404 from express's catch-all.
router.all('/lint', (_req: Request, res: Response) => {
  res
    .status(405)
    .set('Allow', 'POST')
    .json({ error: { code: 'METHOD_NOT_ALLOWED', message: 'Only POST is supported on /lint' } });
});

export default router;
