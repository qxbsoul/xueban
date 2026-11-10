/**
 * skills-preview-triage.ts — Round 4 PR-E compile-status endpoint.
 *
 * `GET /api/skills/:id/compile-status` surfaces whatever the background
 * compile cache currently knows about a skill so the `<SkillDropdown>`
 * UI can render "✅ 已编译 · team · 6 agents · ~$0.12" / "⏳ 编译中..."
 * without the frontend having to read disk paths.
 *
 * Data sources:
 *   1. `getCompiledSkill(skill_id)` — scans .xueban/cache/skill-compile
 *      for a matching CompiledSkill (PR-C cache layer). Single source of
 *      truth for "compiled" / "fallback" status.
 *   2. `compileSkill` in-flight tracker — populated by `markCompiling` /
 *      `markCompileDone` so the dropdown can show "compiling..." while
 *      the boot-warm pass runs. Pure in-memory; survives nothing beyond
 *      the current server process and that's by design.
 *
 * Status values:
 *   - `compiled`  — fresh cache hit; `compiled` field is populated
 *   - `compiling` — background warmer running right now
 *   - `failed`    — last compile attempt logged a failure (mirrors
 *                   `derivedFrom: 'fallback'` from the cache entry)
 *   - `no_cache`  — never compiled, nothing in flight; UI shows neutral
 *
 * Cost estimate: deliberately crude (token-count → USD with
 * provider-neutral price points) so the UI can show ballpark "~$0.12"
 * without us pretending to track real billing. When the cache has
 * `llm_call_meta.model === 'fallback'`, cost is reported as 0 because
 * no real LLM call happened.
 */

import { Router, Request, Response } from 'express';
import { getCompiledSkill } from '../lib/skill-compiler';
import type { CompiledSkill } from '../lib/skill-compiler/types';

const router = Router();

// Same id shape as routes/skills.ts — reject path-traversal / control chars
// before we go anywhere near the filesystem-backed cache.
const VALID_SKILL_ID_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/i;

// ─── in-flight tracker ─────────────────────────────────────────────────────
// Populated by skill-loader's `warmOneSkill` so `compile-status` can show
// "compiling..." before the cache file exists. Plain Set — concurrent
// invocations are gated upstream by the loader.

const inFlight = new Set<string>();

export function markCompiling(skill_id: string): void {
  inFlight.add(skill_id);
}
export function markCompileDone(skill_id: string): void {
  inFlight.delete(skill_id);
}

// ─── cost heuristic ────────────────────────────────────────────────────────
// Crude per-token USD price; matches Round 4 plan spec ($0.003/Ktok in,
// $0.015/Ktok out — anchored to Claude Sonnet-tier pricing). Fallback
// compiles (no LLM call) report $0.

function estimateCostUsd(c: CompiledSkill): number {
  if (c.llm_call_meta.model === 'fallback') return 0;
  const inUsd = (c.llm_call_meta.tokens_in / 1000) * 0.003;
  const outUsd = (c.llm_call_meta.tokens_out / 1000) * 0.015;
  return Number((inUsd + outUsd).toFixed(4));
}

// ─── response shape ────────────────────────────────────────────────────────
// Documented here (not re-exported as a TS type) because the UI consumes
// the JSON directly — adding it to the shared types/ surface would create
// a build-time coupling between server and frontend that PR-E explicitly
// avoids.

interface CompileStatusResponse {
  skill_id: string;
  status: 'compiled' | 'compiling' | 'failed' | 'no_cache';
  compiled?: {
    mode: 'agent' | 'team';
    members_count?: number; // team only
    edges_count?: number;   // team only
    tools_count?: number;   // agent only
    compiled_at: string;
    model: string;
    derived_from?: 'structured' | 'prose-llm' | 'fallback';
  };
  estimated_cost_usd: number;
}

router.get('/:id/compile-status', async (req: Request, res: Response) => {
  const id = req.params.id;
  if (!VALID_SKILL_ID_RE.test(id)) {
    res.status(400).json({ error: 'invalid skill id' });
    return;
  }

  // 1) Cache hit takes precedence — if both inFlight and cache say something
  //    about this skill, the cache is the authoritative latest result.
  const cached = await getCompiledSkill(id).catch(() => null);

  if (cached) {
    const isFallback = cached.teamConfig?.derivedFrom === 'fallback'
      || cached.llm_call_meta.model === 'fallback';
    const body: CompileStatusResponse = {
      skill_id: id,
      status: isFallback ? 'failed' : 'compiled',
      compiled: {
        mode: cached.mode,
        compiled_at: cached.compiled_at,
        model: cached.llm_call_meta.model,
        derived_from: cached.teamConfig?.derivedFrom,
        ...(cached.mode === 'team' && cached.teamConfig
          ? {
              members_count: cached.teamConfig.members_ids.length,
              edges_count: cached.teamConfig.edges_v1.length,
            }
          : {}),
        ...(cached.mode === 'agent' && cached.agentConfig
          ? { tools_count: cached.agentConfig.tools.length }
          : {}),
      },
      estimated_cost_usd: estimateCostUsd(cached),
    };
    res.json(body);
    return;
  }

  // 2) No cache yet — is a compile currently running?
  if (inFlight.has(id)) {
    res.json({
      skill_id: id,
      status: 'compiling',
      estimated_cost_usd: 0,
    } satisfies CompileStatusResponse);
    return;
  }

  // 3) Nothing on disk, nothing in flight — the loader skipped this skill
  //    (no references/ dir) or it was never registered.
  res.json({
    skill_id: id,
    status: 'no_cache',
    estimated_cost_usd: 0,
  } satisfies CompileStatusResponse);
});

export default router;
