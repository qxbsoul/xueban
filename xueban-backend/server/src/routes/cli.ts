/**
 * routes/cli.ts — CLI detector REST endpoints (Story 15.19 v2)
 *
 *   GET  /api/cli/detect           → cached snapshot (re-scans iff cache empty)
 *   POST /api/cli/detect/refresh   → forces a fresh PATH scan
 *
 * Both return `{ scanned_at: ISO8601, items: DetectedCli[] }`.
 */

import { Router, Request, Response } from 'express';
import { detectAll } from '../cli-detector';

const router = Router();

// 2026-05-11 review HIGH-2 (15.19): in-flight singleton + 5s rate limit 防 LAN
// 内攻击者反复触发 detectAll(true) → fork-bomb-lite (10 child_process spawn × N).
// 与 15.10 reload guard 同模式 (OpenDesign).
let refreshInFlight: Promise<unknown> | null = null;
let lastRefreshAt = 0;
const REFRESH_MIN_INTERVAL_MS = 5000;

router.get('/detect', async (_req: Request, res: Response) => {
  try {
    const snap = await detectAll(false);
    res.json(snap);
  } catch (err) {
    res.status(500).json({
      error: 'detect failed',
      message: (err as Error).message,
    });
  }
});

router.post('/detect/refresh', async (_req: Request, res: Response) => {
  // Rate limit: reject if last refresh was within 5s window.
  const now = Date.now();
  if (now - lastRefreshAt < REFRESH_MIN_INTERVAL_MS) {
    res.status(429).json({
      error: 'rate limited',
      retry_after_ms: REFRESH_MIN_INTERVAL_MS - (now - lastRefreshAt),
    });
    return;
  }
  // In-flight singleton: piggyback onto existing scan if one is running.
  if (refreshInFlight) {
    try {
      const snap = await refreshInFlight;
      res.json(snap);
    } catch (err) {
      res.status(500).json({ error: 'refresh failed', message: (err as Error).message });
    }
    return;
  }
  refreshInFlight = detectAll(true);
  try {
    const snap = await refreshInFlight;
    lastRefreshAt = Date.now();
    res.json(snap);
  } catch (err) {
    res.status(500).json({
      error: 'refresh failed',
      message: (err as Error).message,
    });
  } finally {
    refreshInFlight = null;
  }
});

export default router;
