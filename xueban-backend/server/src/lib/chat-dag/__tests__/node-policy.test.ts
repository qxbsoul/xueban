import { describe, it, expect, vi } from 'vitest';
import { runNodeWithPolicy } from '../node-policy';

const hang = (signal: AbortSignal) => new Promise<{ text: string; error?: string }>((_, reject) => {
  signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
});

describe('runNodeWithPolicy', () => {
  it('一次成功 → reason=ok, attempts=1', async () => {
    const r = await runNodeWithPolicy(async () => ({ text: 'hi' }), { timeoutMs: 50, maxRetries: 2, signal: new AbortController().signal });
    expect(r.reason).toBe('ok');
    expect(r.text).toBe('hi');
    expect(r.attempts).toBe(1);
  });

  it('持续返回 error → 用尽重试,reason=error, attempts=maxRetries+1', async () => {
    const call = vi.fn(async () => ({ text: '', error: 'boom' }));
    const r = await runNodeWithPolicy(call, { timeoutMs: 50, maxRetries: 2, signal: new AbortController().signal });
    expect(r.reason).toBe('error');
    expect(r.attempts).toBe(3);
    expect(call).toHaveBeenCalledTimes(3);
  });

  it('先 error 后成功 → reason=ok, attempts=2', async () => {
    let i = 0;
    const r = await runNodeWithPolicy(async () => (i++ === 0 ? { text: '', error: 'x' } : { text: 'ok2' }), { timeoutMs: 50, maxRetries: 3, signal: new AbortController().signal });
    expect(r.reason).toBe('ok');
    expect(r.attempts).toBe(2);
  });

  it('超时(call 挂起超过 timeoutMs)→ reason=timeout(用尽重试)', async () => {
    const r = await runNodeWithPolicy((sig) => hang(sig), { timeoutMs: 50, maxRetries: 1, signal: new AbortController().signal });
    expect(r.reason).toBe('timeout');
    expect(r.attempts).toBe(2); // 初次 + 1 重试都超时
  });

  it('外部 signal 预先 abort → reason=aborted, attempts=0, 不调用 call', async () => {
    const ac = new AbortController();
    ac.abort();
    const call = vi.fn(async () => ({ text: 'x' }));
    const r = await runNodeWithPolicy(call, { timeoutMs: 50, maxRetries: 2, signal: ac.signal });
    expect(r.reason).toBe('aborted');
    expect(r.attempts).toBe(0);
    expect(call).not.toHaveBeenCalled();
  });

  it('运行中外部 abort → reason=aborted,不再重试', async () => {
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 15);
    const r = await runNodeWithPolicy((sig) => hang(sig), { timeoutMs: 1000, maxRetries: 3, signal: ac.signal });
    expect(r.reason).toBe('aborted');
  });

  it('call 同步 throw → 当作 error 重试', async () => {
    const call = vi.fn(async () => { throw new Error('explode'); });
    const r = await runNodeWithPolicy(call, { timeoutMs: 50, maxRetries: 1, signal: new AbortController().signal });
    expect(r.reason).toBe('error');
    expect(r.attempts).toBe(2);
  });
});
