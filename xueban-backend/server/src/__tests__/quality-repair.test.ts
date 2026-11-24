import { describe, expect, it } from 'vitest';
import { repairGeneratedText } from '../services/quality-repair';

describe('学案生成后处理', () => {
  it('修复 2x+5 的 45 误写并清理内部占位', () => {
    const input = '程序按标题候选建立，待 Agent 语义确认。解不等式：\\(\\frac{2x+5}{3}-1<2-x\\)，得 \\(x<45\\)。以上知识点由知识点提取结果补充，需结合教材原文核对。';
    const output = repairGeneratedText(input);
    expect(output).toContain('x<\\frac{4}{5}');
    expect(output).not.toContain('程序按');
    expect(output).not.toContain('知识点提取结果补充');
  });
});