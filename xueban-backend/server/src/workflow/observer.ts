/**
 * workflow/observer.ts — DAG node lifecycle event sink
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - The scheduler ↔ SSE adapter is decoupled through this interface so the
 *     scheduler can be unit-tested without an HTTP layer, and so future
 *     subscribers (metrics, distributed tracing, per-node UI badges) can
 *     plug in without modifying `scheduler.ts`.
 *   - Transport never sees this — it only emits `TurnChunk`s, which the
 *     scheduler then forwards through both the async generator (for SSE
 *     pipelining) and through `onNodeChunk` (for observers that want a
 *     side-channel view).
 *
 * Contract:
 *   - `onNodeStart(id)` is called exactly once before any `onNodeChunk(id, …)`.
 *   - `onNodeEnd(id, result)` is called exactly once, after the last
 *     `onNodeChunk(id, …)` for that node.
 *   - Skipped nodes only get `onNodeStart` + `onNodeEnd` (no chunks).
 *   - Observers MUST NOT throw — the scheduler does not guard against it
 *     and a throw will fail the whole DAG run.
 */

import type { RunResult, TurnChunk } from './types';

export interface NodeObserver {
  /** Fired when a node transitions from `pending` to `running`. */
  onNodeStart(node_id: string): void;
  /**
   * Fired for every chunk yielded by the node's `callable.turn()`. The
   * `chunk.node_id` is guaranteed to equal the first argument, since the
   * scheduler stamps `node_id` before forwarding.
   */
  onNodeChunk(node_id: string, chunk: TurnChunk): void;
  /**
   * Fired exactly once per node, with the final `RunResult`. After this
   * call, no further events for `node_id` will be delivered.
   */
  onNodeEnd(node_id: string, result: RunResult): void;
}

/**
 * No-op observer convenience. Useful for tests, or for code paths that
 * receive the chunk stream through the AsyncIterator only.
 */
export const NULL_OBSERVER: NodeObserver = {
  onNodeStart() {},
  onNodeChunk() {},
  onNodeEnd() {},
};

/**
 * Fan-out helper: dispatch a single event to multiple observers. Errors in
 * any one observer are caught and ignored so a misbehaving subscriber cannot
 * cascade and break siblings. (Observers contractually must not throw, but
 * defence-in-depth is cheap.)
 */
export function multiplex(...observers: NodeObserver[]): NodeObserver {
  return {
    onNodeStart(id) {
      for (const o of observers) {
        try { o.onNodeStart(id); } catch { /* swallow */ }
      }
    },
    onNodeChunk(id, chunk) {
      for (const o of observers) {
        try { o.onNodeChunk(id, chunk); } catch { /* swallow */ }
      }
    },
    onNodeEnd(id, result) {
      for (const o of observers) {
        try { o.onNodeEnd(id, result); } catch { /* swallow */ }
      }
    },
  };
}
