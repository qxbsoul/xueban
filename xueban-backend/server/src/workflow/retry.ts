/**
 * workflow/retry.ts — Per-node retry policy with exponential backoff
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - Orchestration-internal: the scheduler wraps each `executeNode()` call
 *     in `withRetry()` honouring the per-edge `max_retries` from `team.yaml`.
 *   - Transport never sees this; it just throws typed `LlmCallError`s
 *     (Phase 2 decision CL3/E3) which we classify here.
 *
 * Backoff schedule: 500ms, 1s, 2s, 4s, 8s, ... (cap at 30s). When the
 * underlying error carries a `retryAfter` (rate-limit responses), that value
 * supersedes the schedule.
 *
 * Cancellation: the `signal` is checked before every attempt and during
 * sleeps. If the signal is already aborted on entry, we throw immediately
 * without invoking `fn`.
 *
 * Retryable error kinds (Phase 2 decision):
 *   ✓ rate-limit, network, timeout, cli-crash, provider-error
 *   ✗ auth, context-length  (no point retrying — same call will fail)
 */

import { LlmCallError, type LlmCallErrorKind } from './types';

// ─── Classification ──────────────────────────────────────────────────────────

const RETRYABLE: ReadonlySet<LlmCallErrorKind> = new Set<LlmCallErrorKind>([
  'rate-limit',
  'network',
  'timeout',
  'cli-crash',
  'provider-error',
]);

function isRetryable(err: unknown): err is LlmCallError {
  return err instanceof LlmCallError && RETRYABLE.has(err.kind);
}

// ─── Backoff schedule ────────────────────────────────────────────────────────

const BASE_DELAY_MS = 500;
const MAX_DELAY_MS = 30_000;

/**
 * Compute the delay for attempt `n` (0-indexed): 500 * 2^n, capped at 30s.
 * If the error provides a `retryAfter` hint, it wins (still capped at 30s
 * to avoid pathological long sleeps).
 */
function computeDelay(attempt: number, err: unknown): number {
  const hint = err instanceof LlmCallError ? err.retryAfter : undefined;
  if (typeof hint === 'number' && hint > 0) {
    return Math.min(hint, MAX_DELAY_MS);
  }
  return Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
}

// ─── Cancellable sleep ───────────────────────────────────────────────────────

/**
 * Sleep `ms` milliseconds, rejecting early if `signal` aborts.
 * Uses Node's `AbortSignal` (available globally since Node 16).
 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) {
    return Promise.reject(toAbortError(signal));
  }
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(toAbortError(signal));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function toAbortError(signal: AbortSignal): Error {
  // Prefer the platform's reason if present (Node 18+ sets a DOMException-like).
  const reason = (signal as AbortSignal & { reason?: unknown }).reason;
  if (reason instanceof Error) return reason;
  const e = new Error('Aborted');
  e.name = 'AbortError';
  return e;
}

// ─── Public API ──────────────────────────────────────────────────────────────

export interface RetryOptions {
  /**
   * Total retry budget. `maxRetries: 3` means up to 4 attempts (initial +
   * 3 retries). `maxRetries: 0` means no retries (one attempt only).
   */
  maxRetries: number;
  /** Cascading abort signal (Phase 2 decision C1). */
  signal: AbortSignal;
}

/**
 * Run `fn` with exponential backoff retry on retryable errors.
 *
 * - Non-`LlmCallError` exceptions are NOT retried (they likely indicate a
 *   bug in Orchestration code; we want them surfaced immediately).
 * - Non-retryable `LlmCallError`s (auth, context-length) are NOT retried.
 * - On exhaustion, the **last** thrown error is re-thrown so the caller can
 *   yield it as an `error` chunk and mark the node `failed`.
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  opts: RetryOptions,
): Promise<T> {
  if (opts.signal.aborted) throw toAbortError(opts.signal);

  const maxAttempts = Math.max(1, opts.maxRetries + 1);
  let lastError: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (opts.signal.aborted) throw toAbortError(opts.signal);
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (!isRetryable(err)) throw err;
      if (attempt + 1 >= maxAttempts) break; // exhausted
      const delay = computeDelay(attempt, err);
      await sleep(delay, opts.signal);
    }
  }

  // Exhausted retries — re-throw the last error so the caller sees the
  // original typed kind/message.
  throw lastError;
}
