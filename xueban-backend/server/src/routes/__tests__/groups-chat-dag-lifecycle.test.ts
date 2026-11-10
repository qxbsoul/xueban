import { describe, it, expect } from 'vitest';
import { swapDagRun } from '../groups-chat';

describe('swapDagRun', () => {
  it('同 group 第二轮 abort 第一轮、返回新 controller', () => {
    const reg = new Map<string, AbortController>();
    const a = swapDagRun('g1', reg);
    expect(a.signal.aborted).toBe(false);
    const b = swapDagRun('g1', reg);
    expect(a.signal.aborted).toBe(true);
    expect(b.signal.aborted).toBe(false);
    expect(reg.get('g1')).toBe(b);
  });
  it('不同 group 互不影响', () => {
    const reg = new Map<string, AbortController>();
    const a = swapDagRun('g1', reg);
    const b = swapDagRun('g2', reg);
    expect(a.signal.aborted).toBe(false);
    expect(b.signal.aborted).toBe(false);
  });
});
