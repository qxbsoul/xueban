/**
 * perf-bmad-baseline.ts — Scheduler overhead baseline for BMAD-METHOD.
 *
 * Phase 2 doc §3 S3 says "BMAD 13-agent wall-clock 不慢于 today ±20%". Today
 * has no baseline number recorded. This script gives us a deterministic,
 * cheap-to-run **lower bound**: how much of any future wall-clock is the
 * scheduler itself, not the LLM. Real-LLM wall-clock measurement requires
 * tokens + auth and lives in `docs/performance/bmad-baseline.md` as a manual
 * recipe.
 *
 * Usage:
 *   cd server && npx tsx scripts/perf-bmad-baseline.ts
 *
 * Output: JSON with median / p95 wall-clock across N runs of the linear-mode
 * BMAD-METHOD DAG (back-edge stripped per the known scheduler limitation,
 * see docs/qa/bmad-cli-e2e-recipe.md).
 *
 * Note on units: scheduler overhead with a near-zero-latency stub callable
 * is on the order of milliseconds — useful as a regression gate against
 * accidental O(N²) blowups in future scheduler refactors, NOT as a substitute
 * for a real-LLM wall-clock baseline.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { loadTeam } from '../src/lib/team-yaml';
import { runDag } from '../src/workflow/scheduler';
import type { LlmCallable } from '../src/transport/LlmCallable';
import type { TurnChunk } from '../src/workflow/types';

const RUNS = Number.parseInt(process.env.PERF_RUNS ?? '20', 10);
const PER_NODE_DELAY_MS = Number.parseInt(process.env.PERF_NODE_DELAY_MS ?? '50', 10);

function stubCallableWithDelay(delayMs: number): LlmCallable {
  return {
    id: 'perf-stub',
    capabilities: {
      supportsToolUse: false,
      supportsMultiTurn: false,
      supportsStreamingDelta: true,
    },
    async *turn(input: { prompt: string }): AsyncGenerator<TurnChunk> {
      // Simulate a fixed-cost LLM call so wall-clock has a known LLM portion;
      // anything ABOVE (delayMs × node_count) is scheduler overhead.
      if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
      yield { type: 'text-delta', value: `${input.prompt}-output` };
      yield { type: 'done' };
    },
  };
}

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function p95(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const idx = Math.min(s.length - 1, Math.floor(s.length * 0.95));
  return s[idx];
}

async function main(): Promise<void> {
  const { team, errors } = loadTeam('BMAD-METHOD');
  if (!team) {
    console.error('FAIL: cannot load BMAD-METHOD:', errors);
    process.exit(1);
  }
  // Strip back-edge (qa→dev conditional) so the run actually completes —
  // documented limitation in docs/qa/bmad-cli-e2e-recipe.md.
  const linearTeam = {
    ...team,
    edges_v1: team.edges_v1.filter((e) => e.kind !== 'conditional'),
    edges: team.edges.filter((e) => !(e.from === 'qa' && e.to === 'dev')),
  };

  const callable = stubCallableWithDelay(PER_NODE_DELAY_MS);
  const wallClocks: number[] = [];

  // Warm-up
  for (let i = 0; i < 3; i++) {
    const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-perf-warm-'));
    for await (const _c of runDag(linearTeam, callable, ws, new AbortController().signal)) {
      // drain
    }
  }

  for (let i = 0; i < RUNS; i++) {
    const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-perf-'));
    const t0 = performance.now();
    for await (const _c of runDag(linearTeam, callable, ws, new AbortController().signal)) {
      // drain
    }
    wallClocks.push(performance.now() - t0);
  }

  const expectedLlmFloor = PER_NODE_DELAY_MS * linearTeam.members_ids.length;
  const med = median(wallClocks);
  const overhead = med - expectedLlmFloor;

  const result = {
    run_count: RUNS,
    per_node_delay_ms: PER_NODE_DELAY_MS,
    node_count: linearTeam.members_ids.length,
    wall_clock_ms: {
      min: Math.min(...wallClocks),
      median: med,
      p95: p95(wallClocks),
      max: Math.max(...wallClocks),
    },
    expected_llm_floor_ms: expectedLlmFloor,
    scheduler_overhead_ms_median: overhead,
    overhead_ratio: overhead / med,
    recorded_at: new Date().toISOString(),
    git_commit: process.env.GIT_COMMIT ?? '(unknown — set $GIT_COMMIT in CI)',
  };

  console.log(JSON.stringify(result, null, 2));
}

main().catch((err) => {
  console.error('FAIL:', err);
  process.exit(1);
});
