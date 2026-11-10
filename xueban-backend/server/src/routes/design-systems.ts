/**
 * design-systems.ts — Design System listing router (Story 15.5 + Story 15.11).
 *
 * GET  /api/design-systems              → DesignSystemSummary[]
 * GET  /api/design-systems?skill=<id>   → DesignSystemSummary[] filtered by
 *                                          compatible_skills (AC3 — empty
 *                                          array means compatible with all).
 * POST /api/design-systems/reload       → { reloaded, failed, errors, overrides }
 *
 * Returns the public-facing slice of each registered DS (no `injection_prompt`,
 * which is server-only — clients never need it because the prompt is injected
 * at SSE-stream time inside routes/run-sessions.ts).
 */

import { Router, Request, Response } from 'express';
import { listDesignSystems, reloadDesignSystems } from '../design-systems';

const router = Router();

router.get('/', (req: Request, res: Response) => {
  const skill =
    typeof req.query.skill === 'string' && req.query.skill.trim()
      ? req.query.skill.trim()
      : undefined;
  const list = listDesignSystems(skill).map(
    ({ ds_id, name, description, compatible_skills }) => ({
      ds_id,
      name,
      description,
      compatible_skills,
    }),
  );
  res.json(list);
});

/**
 * Story 15.11 AC4 — hot reload of `.xueban/design-systems/*.md`.
 * No-auth endpoint (consistent with /api/skills/reload from Story 15.10).
 */
router.post('/reload', (_req: Request, res: Response) => {
  const result = reloadDesignSystems();
  res.json(result);
});

export default router;
