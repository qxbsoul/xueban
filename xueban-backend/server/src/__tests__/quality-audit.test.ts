import { describe, expect, it } from 'vitest';
import { repairGeneratedText, auditGeneratedText } from '../services/quality-repair';
import { parseBound, intersectBounds, formatIntersection, parseLinear, trySolveLinearInequality } from '../services/algebra-solver';

describe('学案质量审计边界', () => {
  it('两道不同题目不会互相污染答案', () => {
    const input = 'x≥8 与 x<45 没有公共部分\n参考答案：无解';
    const output = repairGeneratedText(input);
    expect(output).toContain('8≤x<45');
    expect(output.split('\n')[1]).toBe('参考答案：无解');
  });

  it('前题有解、后题无解时后题不被前题污染', () => {
    const input = 'x≥8 与 x<45 没有公共部分\n参考答案：无解';
    const output = repairGeneratedText(input);
    expect(output).not.toContain('参考答案：8≤x<45');
  });

  it('前题无解、后题有解时前题结果不被后题覆盖', () => {
    const input = 'x≥8 与 x<4/5 没有公共部分\n参考答案：无解\nx≥8 与 x<45 没有公共部分';
    const output = repairGeneratedText(input);
    expect(output.split('\n')[0]).toContain('没有公共部分');
    expect(output.split('\n')[2]).toContain('8≤x<45');
  });

  it('负数分数和负数小数边界', () => {
    expect(parseBound('x>-1/2')?.value).toEqual({ n: -1, d: 2 });
    expect(parseBound('x<=-2.5')?.value).toEqual({ n: -5, d: 2 });
    expect(parseBound('x<\\dfrac{-1}{2}')?.value).toEqual({ n: -1, d: 2 });
  });

  it('零分母表达式拒绝解析', () => {
    expect(() => parseLinear('\\frac{1}{0}')).toThrow();
    expect(trySolveLinearInequality('\\frac{1}{0}+x<2').ok).toBe(false);
  });

  it('目录和正文相同标题不互相覆盖', () => {
    const content = '## 目录\n#### 单元A\n\n正文A很长，包含例题、理解检查、易错点和本单元小结，内容足够长。\n\n#### 单元A\n\n正文A第二份，包含例题、理解检查、易错点和本单元小结，内容足够长。';
    const audit = auditGeneratedText(content, [{ title: '单元A' }, { title: '单元A' }]);
    expect(audit.missingUnits).toEqual([]);
    expect(audit.emptyUnitCount).toBeLessThanOrEqual(2);
  });

  it('缺失单元和空单元正确区分', () => {
    const content = '#### 单元A\n\n#### 单元B\n\n' + '内容'.repeat(300);
    const audit = auditGeneratedText(content, [{ title: '单元A' }, { title: '单元B' }, { title: '单元C' }]);
    expect(audit.emptyUnitCount).toBe(1);
    expect(audit.missingUnits).toEqual(['单元C']);
  });

  it('无法定位正文时不伪造缺失', () => {
    const audit = auditGeneratedText('只有一段普通正文，没有标题。', [{ title: '单元A' }]);
    expect(audit.missingUnits).toEqual([]);
    expect(audit.unlocatableUnits).toEqual(['单元A']);
  });

  it('无法解析的数学表达不会被自动修改', () => {
    const input = '这是 x^2+1 和一个不确定答案 45。';
    expect(repairGeneratedText(input)).toBe(input);
  });

  it('区间交集端点开闭', () => {
    expect(formatIntersection(intersectBounds(parseBound('x>=8')!, parseBound('x<=8')!))).toBe('8≤x≤8');
  });

  it('没有 #### 但有 ### 教学单元仍可审计', () => {
    const content = '### 2.1 相反数\n\n' + '这是一段足够长的正文。'.repeat(20);
    const audit = auditGeneratedText(content, [{ title: '相反数' }]);
    expect(audit.missingUnits).toEqual([]);
    expect(audit.unlocatableUnits).toEqual([]);
  });

  it('相似标题不会被模糊匹配到别的单元', () => {
    const content = '#### 单元A\n\n' + '内容'.repeat(300) + '\n\n#### 单元AB\n\n' + '内容'.repeat(300);
    const audit = auditGeneratedText(content, [{ title: '单元A' }, { title: '单元B' }]);
    expect(audit.missingUnits).toEqual(['单元B']);
  });

  it('重复段落不能被判定为 complete', () => {
    const body = '重复内容'.repeat(50);
    const content = `#### 单元A\n\n${body}\n\n#### 单元B\n\n${body}`;
    const audit = auditGeneratedText(content, [{ title: '单元A' }, { title: '单元B' }]);
    expect(audit.duplicateParagraphs).toBeGreaterThan(0);
    expect(audit.status).not.toBe('complete');
  });

  it('负数区间自动审计不被自动修改', () => {
    const input = 'x≥-2.5 与 x<-1/2 没有公共部分';
    const output = repairGeneratedText(input);
    expect(output).toContain('\\frac{-5}{2}');
    expect(output).toContain('\\frac{-1}{2}');
  });
});