import { describe, expect, it } from 'vitest';
import { parseLinear, solveLinearInequality, parseBound, intersectBounds, formatIntersection } from '../services/algebra-solver';

describe('一元一次代数求解器', () => {
  it('解析常数乘法、括号和分数', () => {
    expect(parseLinear('2(x+1)')).toEqual({ a: { n: 2, d: 1 }, b: { n: 2, d: 1 } });
    expect(parseLinear('(2x+5)/3-1')).toEqual({ a: { n: 2, d: 3 }, b: { n: 2, d: 3 } });
  });

  it('拒绝二次和非线性乘法', () => {
    expect(() => parseLinear('x^2+1')).toThrow();
    expect(() => parseLinear('x*x')).toThrow();
    expect(() => parseLinear('x/(x+1)')).toThrow();
  });

  it('负系数翻转不等号', () => {
    expect(solveLinearInequality('-2x+3<5')).toEqual({ op: '>', value: { n: -1, d: 1 } });
  });

  it('分数不等式求解', () => {
    expect(solveLinearInequality('(2x+5)/3-1<2-x')).toEqual({ op: '<', value: { n: 4, d: 5 } });
  });

  it('区间交集、空集与端点开闭', () => {
    expect(formatIntersection(intersectBounds(parseBound('x\\ge 8')!, parseBound('x<45')!))).toBe('8≤x<45');
    expect(formatIntersection(intersectBounds(parseBound('x>8')!, parseBound('x<=8')!))).toBe('无解');
    expect(formatIntersection(intersectBounds(parseBound('x>=8')!, parseBound('x<=8')!))).toBe('8≤x≤8');
  });
});