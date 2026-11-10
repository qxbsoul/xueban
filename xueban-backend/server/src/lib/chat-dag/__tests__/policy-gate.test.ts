import { describe, it, expect } from 'vitest';
import { gate } from '../policy-gate';

const M = { pm: { arch: 'permit', qa: 'permit' }, dev: { qa: 'warn' }, arch: { qa: 'deny' } };

describe('gate', () => {
  it('命中 permit', () => { expect(gate(M, 'pm', 'arch')).toBe('permit'); });
  it('命中 warn', () => { expect(gate(M, 'dev', 'qa')).toBe('warn'); });
  it('命中 deny', () => { expect(gate(M, 'arch', 'qa')).toBe('deny'); });
  it('缺 sender 行 → 默认 permit', () => { expect(gate(M, 'unknown', 'qa')).toBe('permit'); });
  it('缺 receiver 列 → 默认 permit', () => { expect(gate(M, 'pm', 'dev')).toBe('permit'); });
  it('空矩阵 → permit', () => { expect(gate({}, 'a', 'b')).toBe('permit'); });
  it('未知值归一为 permit', () => { expect(gate({ a: { b: 'bogus' } }, 'a', 'b')).toBe('permit'); });
  it('null/undefined 矩阵 → permit(防御性)', () => {
    expect(gate(null, 'a', 'b')).toBe('permit');
    expect(gate(undefined, 'a', 'b')).toBe('permit');
  });
});
