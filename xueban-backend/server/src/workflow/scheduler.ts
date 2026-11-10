/**
 * workflow/scheduler.ts — Topological parallel DAG runner (Phase 2 decision A4)
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - This is the **Orchestration entry point** for team-backed skills. The
 *     daemon's run-session route will call `runDag()` once per session and
 *     pipe the yielded `TurnChunk`s into the SSE response.
 *   - Transport is opaque: we hold a single `LlmCallable` and hand it to
 *     every node executor; whether it's an `ApiClientCallable`, `CliCallable`,
 *     or `AcpCallable` is the dispatcher's job (`transport/dispatcher.ts`).
 *
 * Algorithm:
 *   1. Build adjacency + in-degree from `team.edges_v1`.
 *      Conditional edges contribute to in-degree at *node-discovery* time but
 *      are filtered out at *activation* time (see Step 4 below).
 *   2. Kahn-layered scan: pick all nodes whose in-degree == 0 → that's a
 *      topological layer that can run concurrently (Promise.all).
 *   3. For each node in a layer:
 *        - Wrap `executor.executeNode()` in `retry.withRetry()` with the
 *          per-node `max_retries` (taken from the *incoming sequential edge*,
 *          falling back to `team.policy_obj.retry ?? 3`).
 *        - Multiplex chunks into both the shared output queue (so the
 *          async generator can yield them in interleaved order) and the
 *          observer fan-out.
 *   4. After a layer finishes, for each outgoing edge:
 *        - sequential / parallel → activate downstream (decrement in-degree).
 *        - conditional → call `condition.evaluate()`; if true, activate; else
 *          mark the downstream subtree as `skipped`.
 *        - If the upstream node `failed`, all `sequential`/`parallel`
 *          downstream of it become `skipped` too (no partial success).
 *
 * Cancellation (Phase 2 decision C1):
 *   - The caller's `signal` is passed straight into `withRetry()` and
 *     `executor.executeNode()`, which cascade it into `callable.turn()`.
 *   - We don't fork sub-signals; UI node-level cancel is a future feature.
 *
 * Observer:
 *   - The scheduler itself uses the NULL_OBSERVER internally. Callers wanting
 *     to attach an observer should compose it externally and pass it to a
 *     custom variant; in Phase 2 the SSE consumer just iterates the
 *     AsyncGenerator. (We export a second variant below for that use.)
 */

import type {
  NodeContext,
  RunResult,
  TeamDefV1,
  TeamEdgeV1,
  TurnChunk,
} from './types';
import { LlmCallError } from './types';
import type { LlmCallable } from '../transport/LlmCallable';
import type { SkillAgentDef } from '../lib/skill-types';
import { executeNode } from './executor';
import { withRetry } from './retry';
import { evaluate as evaluateCondition } from './condition';
import { multiplex, NULL_OBSERVER, type NodeObserver } from './observer';

// ─── Graph helpers ───────────────────────────────────────────────────────────

interface GraphIndex {
  /** node_id → SkillAgentDef (members already resolved by team-yaml loader). */
  agentById: Map<string, SkillAgentDef>;
  /** node_id → outgoing edges. */
  out: Map<string, TeamEdgeV1[]>;
  /** node_id → incoming edges. */
  in: Map<string, TeamEdgeV1[]>;
}

function buildIndex(team: TeamDefV1): GraphIndex {
  const agentById = new Map<string, SkillAgentDef>();
  for (const a of team.agents) agentById.set(a.id, a);

  const out = new Map<string, TeamEdgeV1[]>();
  const inn = new Map<string, TeamEdgeV1[]>();
  for (const id of team.members_ids) {
    out.set(id, []);
    inn.set(id, []);
  }
  for (const e of team.edges_v1) {
    if (out.has(e.from)) out.get(e.from)!.push(e);
    if (inn.has(e.to)) inn.get(e.to)!.push(e);
  }
  return { agentById, out, in: inn };
}

// ─── Chunk queue (decouples N parallel producers from 1 generator consumer) ──

/**
 * Single-consumer / multi-producer chunk queue used to interleave streams
 * from parallel node executors. Producers call `push()`; the consumer
 * `await`s `pull()` which returns the next chunk or `null` when closed and
 * empty.
 */
class ChunkQueue {
  private buf: TurnChunk[] = [];
  private waiter: ((v: TurnChunk | null) => void) | null = null;
  private closed = false;

  push(chunk: TurnChunk): void {
    if (this.closed) return;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w(chunk);
    } else {
      this.buf.push(chunk);
    }
  }

  /** Resolve waiter (if any) with null and refuse further pushes. */
  close(): void {
    this.closed = true;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w(null);
    }
  }

  pull(): Promise<TurnChunk | null> {
    if (this.buf.length > 0) {
      return Promise.resolve(this.buf.shift()!);
    }
    if (this.closed) return Promise.resolve(null);
    return new Promise((resolve) => {
      this.waiter = resolve;
    });
  }
}

// ─── Per-node max_retries resolution ─────────────────────────────────────────

/**
 * Pick the retry budget for a node. The Phase 2 contract is "incoming edge
 * carries the max_retries hint"; we pick the **max** across incoming edges
 * (most permissive) and fall back to the team-level retry policy.
 */
function resolveMaxRetries(team: TeamDefV1, incoming: TeamEdgeV1[]): number {
  const explicit = incoming
    .map((e) => e.max_retries)
    .filter((n): n is number => typeof n === 'number');
  if (explicit.length > 0) return Math.max(...explicit);
  return team.policy_obj?.retry ?? 3;
}

// ─── Skipped-result helper ───────────────────────────────────────────────────

function skippedResult(node_id: string): RunResult {
  return { node_id, status: 'skipped', artifacts: [], durationMs: 0 };
}

// ─── Core run loop (parameterised on observer for testability) ───────────────

async function* runDagInternal(
  team: TeamDefV1,
  callable: LlmCallable,
  workspace: string,
  signal: AbortSignal,
  observer: NodeObserver,
  goal?: string,
  sessionId?: string,
): AsyncGenerator<TurnChunk> {
  const idx = buildIndex(team);
  const queue = new ChunkQueue();

  // Per-node results accumulator (shared across layers — handed to each
  // NodeContext so conditions can inspect prior runs).
  const results = new Map<string, RunResult>();

  // Track each node's *effective* in-degree (conditional edges drop out
  // dynamically depending on upstream outcome).
  const indeg = new Map<string, number>();
  for (const id of team.members_ids) {
    indeg.set(id, idx.in.get(id)?.length ?? 0);
  }

  // Producer pump: collects from queue and yields to caller. The whole
  // pipeline relies on the fact that close() will resolve the pending pull
  // with null so this generator terminates cleanly.
  const pump = (async function* () {
    while (true) {
      const c = await queue.pull();
      if (c === null) return;
      yield c;
    }
  })();

  // Observer wrapper that also pushes every chunk into the queue.
  const queueingObserver: NodeObserver = multiplex(observer, {
    onNodeStart() {},
    onNodeChunk(_id, chunk) { queue.push(chunk); },
    onNodeEnd() {},
  });

  // Run loop driver — does NOT await chunk yielding; it just kicks off
  // layers and stores results. Errors from individual nodes become
  // RunResult.error and are also surfaced as `error` chunks via the queue.
  const driver = (async () => {
    try {
      while (true) {
        if (signal.aborted) break;

        // Pick all currently-ready nodes (in-degree 0 AND not yet scheduled).
        const ready: string[] = [];
        for (const id of team.members_ids) {
          if (!results.has(id) && indeg.get(id) === 0) {
            ready.push(id);
          }
        }
        if (ready.length === 0) break; // either done or stuck (skipped/cycle)

        // Mark them all as "scheduled" by pre-seeding a `pending` marker so
        // subsequent loop iterations don't pick them again. We replace the
        // marker with the real RunResult after the executor returns.
        for (const id of ready) {
          results.set(id, { node_id: id, status: 'pending', artifacts: [], durationMs: 0 });
        }

        await Promise.all(
          ready.map(async (id) => {
            const agent = idx.agentById.get(id);
            if (!agent) {
              // Should never happen — team-yaml validates this — but fail
              // gracefully if it does.
              const r: RunResult = {
                node_id: id,
                status: 'failed',
                artifacts: [],
                error: new LlmCallError('provider-error', `agent "${id}" not resolved`),
                durationMs: 0,
              };
              results.set(id, r);
              queue.push({ type: 'error', error: r.error!, node_id: id });
              return;
            }

            const ctx: NodeContext = {
              team,
              node_id: id,
              workspace,
              // Snapshot of prior results at the moment this node starts.
              // (Layers are sequential, so this only contains nodes from
              // earlier layers — exactly what conditions expect.)
              priorResults: new Map(results),
              goal,
              // 阶段 1 — 审批关联：团队节点内 prompt 工具的 permission_check
              // 帧要落到正确的 run 事件日志，executor 需要知道 session id。
              session_id: sessionId,
            };

            const incoming = idx.in.get(id) ?? [];
            const maxRetries = resolveMaxRetries(team, incoming);

            let result: RunResult;
            try {
              result = await withRetry(
                () => executeNode(agent, ctx, callable, queueingObserver, signal),
                { maxRetries, signal },
              );
            } catch (err) {
              // Retry exhausted or non-retryable throw escaped executor's
              // catch (shouldn't, since executor never throws — but defence
              // in depth). Convert to a failed RunResult.
              const llmErr =
                err instanceof LlmCallError
                  ? err
                  : new LlmCallError('provider-error', (err as Error)?.message ?? String(err), { cause: err });
              result = {
                node_id: id,
                status: 'failed',
                artifacts: [],
                error: llmErr,
                durationMs: 0,
              };
              queue.push({ type: 'error', error: llmErr, node_id: id });
            }

            results.set(id, result);
          }),
        );

        // After the layer finishes, walk outgoing edges and update in-degree
        // / skip cascade.
        for (const id of ready) {
          const result = results.get(id)!;
          const outgoing = idx.out.get(id) ?? [];

          for (const e of outgoing) {
            const downstreamId = e.to;

            // Edge "activation" semantics:
            //  - sequential / parallel: active iff upstream is `done`.
            //    If upstream failed/skipped → propagate skip.
            //  - conditional: must evaluate condition on top of `done`.
            const kind = e.kind ?? 'sequential';

            if (kind === 'conditional') {
              // Conditional edges only consider the `condition` predicate
              // if upstream produced a result we can talk about (done OR
              // failed — letting the condition gate on `prev.x.error`).
              const ctx: NodeContext = {
                team,
                node_id: downstreamId,
                workspace,
                priorResults: new Map(results),
                session_id: sessionId,
              };
              const passed = result.status !== 'skipped'
                && evaluateCondition(e.condition ?? '', ctx);
              if (passed) {
                indeg.set(downstreamId, (indeg.get(downstreamId) ?? 1) - 1);
              } else {
                // Don't decrement in-degree; instead, treat this edge as
                // "removed" by decrementing AND pre-marking downstream as
                // skipped (so further activation attempts no-op). This way
                // a node with multiple conditional parents only runs if
                // ALL its conditional gates resolve.
                indeg.set(downstreamId, (indeg.get(downstreamId) ?? 1) - 1);
                cascadeSkip(downstreamId, idx, results, queue, observer);
              }
            } else {
              // sequential / parallel
              if (result.status === 'done') {
                indeg.set(downstreamId, (indeg.get(downstreamId) ?? 1) - 1);
              } else {
                // failed or skipped → cascade skip
                indeg.set(downstreamId, (indeg.get(downstreamId) ?? 1) - 1);
                cascadeSkip(downstreamId, idx, results, queue, observer);
              }
            }
          }
        }
      }
    } finally {
      queue.close();
    }
  })();

  // Yield chunks until the queue is closed; meanwhile the driver runs in the
  // background. We don't `await driver` until after we drain because that
  // would deadlock — the driver only `close()`s the queue at the end.
  try {
    for await (const chunk of pump) {
      yield chunk;
    }
  } finally {
    // Ensure driver settles (and propagates any unexpected error).
    await driver;
  }
}

/**
 * Mark `id` and its (sequential/parallel) downstream subtree as skipped.
 * Recurses through `out` edges; conditional edges are not auto-skipped — we
 * leave them for the main loop to gate via `evaluate()`.
 *
 * If a node is already in `results` we leave it alone (it ran in an earlier
 * layer, or is currently running).
 */
function cascadeSkip(
  id: string,
  idx: GraphIndex,
  results: Map<string, RunResult>,
  queue: ChunkQueue,
  observer: NodeObserver,
): void {
  // Only skip if this node hasn't been seen yet.
  const existing = results.get(id);
  if (existing && existing.status !== 'pending') return;

  const r = skippedResult(id);
  results.set(id, r);
  observer.onNodeStart(id);
  observer.onNodeEnd(id, r);
  queue.push({ type: 'done', node_id: id });

  const outgoing = idx.out.get(id) ?? [];
  for (const e of outgoing) {
    const kind = e.kind ?? 'sequential';
    if (kind === 'conditional') continue; // gate decides separately
    cascadeSkip(e.to, idx, results, queue, observer);
  }
}

// ─── Public API ──────────────────────────────────────────────────────────────

/**
 * Run a team-yaml DAG with the supplied transport callable. Yields a single
 * interleaved stream of `TurnChunk`s, each tagged with `node_id` so the SSE
 * consumer (parser.ts, route handler) can route them to the correct UI
 * surface.
 *
 * The generator completes when:
 *   - every node has reached a terminal state (done / failed / skipped), OR
 *   - the supplied `signal` is aborted (downstream callable.turn() will
 *     observe the signal and end their streams).
 *
 * This signature deliberately omits an observer parameter for the public
 * default; SSE consumers only need the AsyncGenerator. Callers that want a
 * side-channel of node lifecycle events should use `runDagWithObserver()`.
 */
export function runDag(
  team: TeamDefV1,
  callable: LlmCallable,
  workspace: string,
  signal: AbortSignal,
  goal?: string,
  sessionId?: string,
): AsyncGenerator<TurnChunk> {
  return runDagInternal(team, callable, workspace, signal, NULL_OBSERVER, goal, sessionId);
}

/**
 * Same as `runDag` but with an explicit observer for lifecycle events.
 * Useful for daemon-side metrics, per-node SQLite persistence, or future
 * checkpoint/resume hooks.
 */
export function runDagWithObserver(
  team: TeamDefV1,
  callable: LlmCallable,
  workspace: string,
  signal: AbortSignal,
  observer: NodeObserver,
  goal?: string,
  sessionId?: string,
): AsyncGenerator<TurnChunk> {
  return runDagInternal(team, callable, workspace, signal, observer, goal, sessionId);
}
