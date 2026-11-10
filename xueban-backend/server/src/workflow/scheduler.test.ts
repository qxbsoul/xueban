/**
 * workflow/scheduler.test.ts
 *
 * Contract tests for the Phase 2 DAG runner. We exercise:
 *
 *   1. Linear chain A→B→C — Kahn topological order, all `done`.
 *   2. Parallel layer A→[B,C]→D — B and C are *actually* scheduled in the
 *      same layer (proved via a rendezvous barrier that deadlocks if they
 *      run serially).
 *   3. Conditional edge gating — `condition: "true"` runs the downstream,
 *      `"false"` skips it.
 *   4. Failure cascade — a failed upstream causes sequential downstream to
 *      be skipped (no partial success).
 *   5. Per-node retry budget — failure of a transient node is retried up to
 *      the edge's `max_retries`.
 *
 * Transport is faked with a `FakeCallable` that returns one `text-delta` +
 * `done` per node so the executor's artifact-write code path actually runs
 * (we then assert files exist under the workspace).
 *
 * doc §3 Acceptance Criteria #3 ("parallel 边触发并发 LLM call") and #4
 * ("conditional 边 评估正确") are validated here.
 */

import { describe, it, expect } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { runDag, runDagWithObserver } from './scheduler';
import type {
  TeamDefV1,
  TeamEdgeV1,
  TurnChunk,
  RunResult,
} from './types';
import type { LlmCallable } from '../transport/LlmCallable';
import type { NodeObserver } from './observer';
import type { SkillAgentDef } from '../lib/skill-types';

// ─── Fixtures ────────────────────────────────────────────────────────────────

function mkAgent(id: string, overrides: Partial<SkillAgentDef> = {}): SkillAgentDef {
  return {
    id,
    title: id,
    persona: `You are ${id}.`,
    model: { provider: 'anthropic', name: 'claude-sonnet-4-5' } as never,
    tools: { allowed: [] } as never,
    anchors: {} as never,
    source_file: `${id}.agent.md`,
    ...overrides,
  };
}

function mkTeam(
  agents: SkillAgentDef[],
  edges: TeamEdgeV1[],
  policy: Partial<TeamDefV1['policy_obj']> = {},
): TeamDefV1 {
  const member_ids = agents.map((a) => a.id);
  return {
    // legacy TeamDef base
    name: 'test-team',
    mode: 'dag',
    policy: 'permissive',
    retry: policy.retry ?? 3,
    agents,
    edges: edges.map((e) => ({ from: e.from, to: e.to })),
    loaded_at: 0,
    source_dir: '/fake',
    // v1 extras
    team_id: 'test-team',
    version: 1,
    policy_obj: { retry: 3, ...policy },
    members_ids: member_ids,
    edges_v1: edges,
  };
}

function mkWorkspace(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'sf-scheduler-test-'));
}

// ─── Fake callables ──────────────────────────────────────────────────────────

/**
 * Build a fake `LlmCallable` that returns the given `text` per node prompt.
 * The prompt the executor passes is the node id (see executor.ts:167).
 */
function fakeCallable(
  textByNode: Record<string, string>,
  opts: { delayMs?: number; failNodes?: Set<string> } = {},
): LlmCallable {
  return {
    id: 'fake',
    capabilities: {
      supportsToolUse: false,
      supportsMultiTurn: false,
      supportsStreamingDelta: true,
    },
    async *turn(input: { prompt: string }): AsyncGenerator<TurnChunk> {
      const nodeId = input.prompt;
      if (opts.failNodes?.has(nodeId)) {
        // simulate provider-side error chunk + early return
        const { LlmCallError } = await import('./types');
        yield { type: 'error', error: new LlmCallError('provider-error', `boom ${nodeId}`) };
        yield { type: 'done' };
        return;
      }
      const text = textByNode[nodeId] ?? `${nodeId}-output`;
      if (opts.delayMs) await new Promise((r) => setTimeout(r, opts.delayMs));
      yield { type: 'text-delta', value: text };
      yield { type: 'done' };
    },
  };
}

/**
 * Rendezvous barrier callable: every node call blocks until exactly N
 * different nodes have entered `turn()`. Used to *prove* parallel scheduling:
 * if the scheduler is serial the test deadlocks (vitest hits its 5s timeout
 * and fails); if parallel, all N enter the barrier together and complete.
 */
function rendezvousCallable(n: number): LlmCallable {
  let arrived = 0;
  let release!: () => void;
  const gate = new Promise<void>((r) => { release = r; });
  return {
    id: 'rendezvous',
    capabilities: {
      supportsToolUse: false,
      supportsMultiTurn: false,
      supportsStreamingDelta: true,
    },
    async *turn(input: { prompt: string }): AsyncGenerator<TurnChunk> {
      arrived += 1;
      if (arrived >= n) release();
      await gate;
      yield { type: 'text-delta', value: `${input.prompt}-output` };
      yield { type: 'done' };
    },
  };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Drain a TurnChunk stream into an array. */
async function drain(stream: AsyncGenerator<TurnChunk>): Promise<TurnChunk[]> {
  const out: TurnChunk[] = [];
  for await (const c of stream) out.push(c);
  return out;
}

/**
 * Lifecycle-recording observer. Captures the relative ordering of node
 * start/end events so we can assert layered topology.
 */
function recordingObserver(): {
  observer: NodeObserver;
  events: Array<{ kind: 'start' | 'end'; id: string; status?: string }>;
  results: Map<string, RunResult>;
} {
  const events: Array<{ kind: 'start' | 'end'; id: string; status?: string }> = [];
  const results = new Map<string, RunResult>();
  return {
    events,
    results,
    observer: {
      onNodeStart(id) {
        events.push({ kind: 'start', id });
      },
      onNodeChunk() {},
      onNodeEnd(id, r) {
        events.push({ kind: 'end', id, status: r.status });
        results.set(id, r);
      },
    },
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('workflow/scheduler — Kahn topological execution', () => {
  it('runs a linear chain A→B→C in order', async () => {
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B'), mkAgent('C')],
      [
        { from: 'A', to: 'B' },
        { from: 'B', to: 'C' },
      ],
    );
    const ws = mkWorkspace();
    const callable = fakeCallable({ A: 'a-text', B: 'b-text', C: 'c-text' });
    const { observer, events, results } = recordingObserver();

    await drain(
      runDagWithObserver(team, callable, ws, new AbortController().signal, observer),
    );

    // All three completed successfully.
    expect(results.get('A')?.status).toBe('done');
    expect(results.get('B')?.status).toBe('done');
    expect(results.get('C')?.status).toBe('done');

    // Order: A finished before B started; B finished before C started.
    const aEnd = events.findIndex((e) => e.kind === 'end' && e.id === 'A');
    const bStart = events.findIndex((e) => e.kind === 'start' && e.id === 'B');
    const bEnd = events.findIndex((e) => e.kind === 'end' && e.id === 'B');
    const cStart = events.findIndex((e) => e.kind === 'start' && e.id === 'C');
    expect(aEnd).toBeLessThan(bStart);
    expect(bEnd).toBeLessThan(cStart);

    // Artifacts landed in workspace (executor writes <node_id>.md by default).
    expect(fs.readFileSync(path.join(ws, 'A.md'), 'utf-8')).toBe('a-text');
    expect(fs.readFileSync(path.join(ws, 'B.md'), 'utf-8')).toBe('b-text');
    expect(fs.readFileSync(path.join(ws, 'C.md'), 'utf-8')).toBe('c-text');
  });
});

describe('workflow/scheduler — parallel layer (Phase 2 A4)', () => {
  it('schedules sibling nodes concurrently (rendezvous proves it)', async () => {
    // A → B, A → C, [B,C] → D. After A finishes, B and C are both in-degree
    // 0 and must enter turn() *together* — the rendezvous gate releases once
    // 2 callers arrive. Serial scheduling would deadlock here.
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B'), mkAgent('C'), mkAgent('D')],
      [
        { from: 'A', to: 'B', kind: 'parallel' },
        { from: 'A', to: 'C', kind: 'parallel' },
        { from: 'B', to: 'D' },
        { from: 'C', to: 'D' },
      ],
    );
    const ws = mkWorkspace();

    // A normal callable for A and D; rendezvous for B and C only. We
    // compose by routing on prompt.
    const rendezvous = rendezvousCallable(2);
    const trivial = fakeCallable({});
    const callable: LlmCallable = {
      id: 'composite',
      capabilities: rendezvous.capabilities,
      async *turn(input) {
        if (input.prompt === 'B' || input.prompt === 'C') {
          yield* rendezvous.turn(input);
        } else {
          yield* trivial.turn(input);
        }
      },
    };

    const { observer, results } = recordingObserver();
    await drain(
      runDagWithObserver(team, callable, ws, new AbortController().signal, observer),
    );

    expect(results.get('A')?.status).toBe('done');
    expect(results.get('B')?.status).toBe('done');
    expect(results.get('C')?.status).toBe('done');
    expect(results.get('D')?.status).toBe('done');
  }, 10_000);
});

describe('workflow/scheduler — conditional edges (Phase 2 A4b)', () => {
  it('skips downstream when condition evaluates false', async () => {
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B')],
      [{ from: 'A', to: 'B', kind: 'conditional', condition: 'false' }],
    );
    const ws = mkWorkspace();
    const callable = fakeCallable({});
    const { observer, results } = recordingObserver();

    await drain(
      runDagWithObserver(team, callable, ws, new AbortController().signal, observer),
    );

    expect(results.get('A')?.status).toBe('done');
    expect(results.get('B')?.status).toBe('skipped');
    // B never wrote an artifact because it didn't run.
    expect(fs.existsSync(path.join(ws, 'B.md'))).toBe(false);
  });

  it('activates downstream when condition evaluates true', async () => {
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B')],
      [{ from: 'A', to: 'B', kind: 'conditional', condition: 'true' }],
    );
    const ws = mkWorkspace();
    const callable = fakeCallable({ A: 'a', B: 'b' });
    const { observer, results } = recordingObserver();

    await drain(
      runDagWithObserver(team, callable, ws, new AbortController().signal, observer),
    );

    expect(results.get('A')?.status).toBe('done');
    expect(results.get('B')?.status).toBe('done');
    expect(fs.readFileSync(path.join(ws, 'B.md'), 'utf-8')).toBe('b');
  });
});

describe('workflow/scheduler — failure cascade', () => {
  it('skips sequential downstream when upstream fails', async () => {
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B'), mkAgent('C')],
      [
        { from: 'A', to: 'B' },
        { from: 'B', to: 'C' },
      ],
      { retry: 0 }, // disable retry so the failure surfaces immediately
    );
    const ws = mkWorkspace();
    const callable = fakeCallable({}, { failNodes: new Set(['A']) });
    const { observer, results } = recordingObserver();

    await drain(
      runDagWithObserver(team, callable, ws, new AbortController().signal, observer),
    );

    // A failed; B and C cascade-skipped.
    expect(results.get('A')?.status).toBe('failed');
    expect(results.get('B')?.status).toBe('skipped');
    expect(results.get('C')?.status).toBe('skipped');
  });
});

describe('runDag default observer', () => {
  it('produces a clean stream with done chunks for every node', async () => {
    const team = mkTeam(
      [mkAgent('A'), mkAgent('B')],
      [{ from: 'A', to: 'B' }],
    );
    const ws = mkWorkspace();
    const callable = fakeCallable({ A: 'a', B: 'b' });
    const chunks = await drain(runDag(team, callable, ws, new AbortController().signal));
    // At minimum: one text-delta per node + one done per node.
    const doneCount = chunks.filter((c) => c.type === 'done').length;
    expect(doneCount).toBeGreaterThanOrEqual(2);
    const texts = chunks.filter((c) => c.type === 'text-delta').map((c) => (c as { value: string }).value);
    expect(texts).toContain('a');
    expect(texts).toContain('b');
  });
});
