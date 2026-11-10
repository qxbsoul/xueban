/**
 * rework-detect.test.ts — 零 LLM 驳回判定测试。
 */
import { describe, it, expect } from 'vitest';
import { detectRejection, REWORK_MARKERS } from '../rework-detect';

describe('detectRejection', () => {
  it('硬信号 [verdict:reject] 命中（大小写不敏感）', () => {
    expect(detectRejection('质量不行 [VERDICT:REJECT] 请修改')).toBe(true);
    expect(detectRejection('[verdict: reject]')).toBe(true);
  });

  it('硬信号 needs_rework / needs-rework 命中', () => {
    expect(detectRejection('NEEDS_REWORK: 第二段缺少数据来源')).toBe(true);
    expect(detectRejection('the draft needs-rework on section 2')).toBe(true);
  });

  it('中文软信号：驳回/打回/不通过/需要返工 命中', () => {
    expect(detectRejection('这份方案整体不错，但第三条预算驳回重做。')).toBe(true);
    expect(detectRejection('测试不通过，存在边界溢出。')).toBe(true);
    expect(detectRejection('第 2 版仍需返工')).toBe(true);
  });

  it('通过文本不误报', () => {
    expect(detectRejection('验收通过，可以交付。')).toBe(false);
    expect(detectRejection('整体符合预期，无阻塞问题。 LGTM')).toBe(false);
    // 「通过」单独出现不是信号；「不通过」才是
    expect(detectRejection('上线检查清单已全部通过')).toBe(false);
  });

  it('空/空白文本 = 通过（评审没说话不构成驳回）', () => {
    expect(detectRejection('')).toBe(false);
    expect(detectRejection('   \n  ')).toBe(false);
  });

  it('标记表本身无重复、全为非空串', () => {
    expect(new Set(REWORK_MARKERS).size).toBe(REWORK_MARKERS.length);
    expect(REWORK_MARKERS.every((m) => m.length > 0)).toBe(true);
  });
});
