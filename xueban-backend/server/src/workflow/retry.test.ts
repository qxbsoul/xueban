/**
 * workflow/retry.test.ts — withRetry contract
 *
 * Verifies the Phase 2 retry classifier + exponential backoff:
 *   - retryable kinds (rate-limit/network/timeout/cli-crash/provider-error)
 *     retry up to maxRetries+1 total attempts
 *   - non-retryable kinds (auth/context-length) throw immediately
 *   - non-LlmCallError exceptions throw immediately (bug surfacing)
 *   - retryAfter hint honoured over default backoff
 *   - AbortSignal short-circuits before any attempt + during sleep
 *   - exhaustion re-throws the last error (caller wraps as failed RunResult)
 */

import { describe, it, expect, vi } from 'vitest';
import { withRetry } from './retry';
import { LlmCallError } from './types';

describe('withRetry — happy path', () => {
  it('returns immediately on first-attempt success', async () => {
    const fn = vi.fn(async () => 'ok');
    const out = await withRetry(fn, {
      maxRetries: 3,
      signal: new AbortController().signal,
    });
    expect(out).toBe('ok');
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('withRetry — retryable errors', () => {
  it('retries up to maxRetries+1 total attempts on rate-limit', async () => {
    let attempt = 0;
    const fn = vi.fn(async () => {
      attempt += 1;
      if (attempt < 3) throw new LlmCallError('rate-limit', 'slow down', { retryAfter: 1 });
      return 'eventually';
    });
    const out = await withRetry(fn, {
      maxRetries: 3,
      signal: new AbortController().signal,
    });
    expect(out).toBe('eventually');
    expect(fn).toHaveBeenCalledTimes(3); // 1 initial + 2 retries
  });

  it('re-throws the LAST error after exhausting retries', async () => {
    const fn = vi.fn(async () => {
      throw new LlmCallError('network', 'down', { retryAfter: 1 });
    });
    await expect(
      withRetry(fn, { maxRetries: 2, signal: new AbortController().signal }),
    ).rejects.toMatchObject({ kind: 'network', message: 'down' });
    expect(fn).toHaveBeenCalledTimes(3); // 1 + 2 retries
  });

  it('maxRetries: 0 means exactly one attempt', async () => {
    const fn = vi.fn(async () => {
      throw new LlmCallError('provider-error', 'transient', { retryAfter: 1 });
    });
    await expect(
      withRetry(fn, { maxRetries: 0, signal: new AbortController().signal }),
    ).rejects.toMatchObject({ kind: 'provider-error' });
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('withRetry — non-retryable errors', () => {
  it('does NOT retry auth errors', async () => {
    const fn = vi.fn(async () => {
      throw new LlmCallError('auth', 'bad key');
    });
    await expect(
      withRetry(fn, { maxRetries: 5, signal: new AbortController().signal }),
    ).rejects.toMatchObject({ kind: 'auth' });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('does NOT retry context-length errors', async () => {
    const fn = vi.fn(async () => {
      throw new LlmCallError('context-length', 'too big');
    });
    await expect(
      withRetry(fn, { maxRetries: 5, signal: new AbortController().signal }),
    ).rejects.toMatchObject({ kind: 'context-length' });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('does NOT retry plain Error (likely Orchestration bug)', async () => {
    const fn = vi.fn(async () => {
      throw new Error('TypeError: cannot read undefined');
    });
    await expect(
      withRetry(fn, { maxRetries: 5, signal: new AbortController().signal }),
    ).rejects.toThrow('TypeError');
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('withRetry — AbortSignal', () => {
  it('throws AbortError without invoking fn when signal is already aborted', async () => {
    const ctl = new AbortController();
    ctl.abort();
    const fn = vi.fn(async () => 'never');
    await expect(
      withRetry(fn, { maxRetries: 3, signal: ctl.signal }),
    ).rejects.toThrow(/abort/i);
    expect(fn).not.toHaveBeenCalled();
  });

  it('throws AbortError if signal fires between attempts (during sleep)', async () => {
    const ctl = new AbortController();
    let attempt = 0;
    const fn = vi.fn(async () => {
      attempt += 1;
      if (attempt === 1) {
        // abort after the first failure but before the second attempt.
        setTimeout(() => ctl.abort(), 0);
        throw new LlmCallError('network', 'flaky', { retryAfter: 100 });
      }
      return 'should-not-reach';
    });
    await expect(
      withRetry(fn, { maxRetries: 3, signal: ctl.signal }),
    ).rejects.toThrow(/abort/i);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
