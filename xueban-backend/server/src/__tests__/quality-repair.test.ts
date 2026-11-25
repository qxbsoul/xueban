import { describe, expect, it } from 'vitest';
import { auditGeneratedText, repairGeneratedText } from '../services/quality-repair';

describe('学案生成后处理', () => {
  it('清理内部占位并修复可确定的一次不等式', () => {
    const input = '程序按标题候选建立，待 Agent 语义确认。解不等式：\\(\\frac{2x+5}{3}-1<2-x\\)，得 \\(x<45\\)。以上知识点由知识点提取结果补充，需结合教材原文核对。';
    const output = repairGeneratedText(input);
    expect(output).not.toContain('程序按');
    expect(output).not.toContain('知识点提取结果补充');
    expect(output).toContain('x<\\frac{4}{5}');
  });

  it('审计空单元、内部状态和待复核数学表达', () => {
    const audit = auditGeneratedText('#### 单元一\n\n程序按标题候选建立。\n\n#### 单元二\n\n只有一句话。', [
      { title: '单元一' }, { title: '单元二' },
    ]);
    expect(audit.internalPlaceholders).toBeGreaterThan(0);
    expect(audit.status).not.toBe('complete');
    expect(audit.emptyUnitCount + audit.missingUnits.length).toBeGreaterThan(0);
  });
});
