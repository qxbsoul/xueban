/**
 * routes/acp.ts — ACP / MCP detector REST endpoints (Story 15.23)
 *
 *   GET  /api/acp/detect           → cached snapshot (re-scans iff cache empty)
 *   POST /api/acp/detect/refresh   → forces a fresh PATH + TCP-ping scan
 *
 * Mirrors `routes/cli.ts` shape so the front-end pattern is identical.
 */

import { Router, type Request, type Response } from 'express';
import { detectAcpAgents, getCachedAcpSnapshot } from '../acp-detector';

const router = Router();

router.get('/detect', async (_req: Request, res: Response) => {
  try {
    const cached = getCachedAcpSnapshot();
    const snap = cached ?? (await detectAcpAgents(true));
    res.json(snap);
  } catch (err) {
    res.status(500).json({ error: 'detect failed', message: (err as Error).message });
  }
});

router.post('/detect/refresh', async (_req: Request, res: Response) => {
  try {
    const snap = await detectAcpAgents(true);
    res.json(snap);
  } catch (err) {
    res.status(500).json({ error: 'refresh failed', message: (err as Error).message });
  }
});

export default router;
