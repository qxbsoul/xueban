/**
 * 结构化解析测试（2026-11-15）。
 * 覆盖目录层级、知识点与出处、公式/表格/图片保留、无法识别时的待确认状态。
 */
import { describe, expect, it } from 'vitest';
import { buildLessonMarkdown, parseStructuredDocument } from '../services/learning-parser';

const SAMPLE = [
  '第一章 有理数',
  '学习目标',
  '- 理解正数和负数的意义',
  '- 会用数轴表示有理数',
  '',
  '1.1 正数和负数',
  '知识点：正数与负数的概念',
  '正数和负数是表示相反意义的量。',
  '定义：大于零的数叫正数，小于零的数叫负数。',
  '温度上升记作 +3，下降记作 -3。',
  '',
  '知识点：数轴',
  '规定了原点、正方向和单位长度的直线叫数轴。',
  '公式：a + (-a) = 0',
  '',
  '1.2 有理数的加减法',
  '同号两数相加，取相同的符号，并把绝对值相加。',
  '',
  '| 运算 | 符号 |',
  '| --- | --- |',
  '| 同号 | 保留 |',
  '',
  '![数轴示意图](images/number-line.png)',
].join('\n');

describe('结构化教材解析', () => {
  it('解析出章 / 节 / 知识点三层，知识点不会变成章节标题', () => {
    const doc = parseStructuredDocument(SAMPLE);
    expect(doc.chapters).toHaveLength(1);
    expect(doc.chapters[0].title).toBe('有理数');
    expect(doc.chapters[0].sections).toHaveLength(2);
    expect(doc.chapters[0].sections[0].title).toBe('正数和负数');
    expect(doc.chapters[0].sections[0].knowledge_points.map((point) => point.title))
      .toEqual(['正数与负数的概念', '数轴']);
    expect(doc.chapters[0].sections[0].knowledge_points[0].status).toBe('parsed');
  });

  it('保留原始行号与知识点来源块，能回到教材位置', () => {
    const doc = parseStructuredDocument(SAMPLE);
    const point = doc.chapters[0].sections[0].knowledge_points[0];
    expect(point.block_keys.length).toBeGreaterThan(0);
    const blocks = point.block_keys.map((key) => doc.blocks.find((block) => block.key === key)!);
    expect(blocks.every((block) => block.line_start > 0 && block.line_end >= block.line_start)).toBe(true);
    expect(blocks.map((block) => block.text).join('\n')).toContain('正数和负数是表示相反意义的量');
    expect(blocks[0].line_start).toBe(8);
  });

  it('学习目标、概念、公式、表格、图片按原文保留', () => {
    const doc = parseStructuredDocument(SAMPLE);
    const section = doc.chapters[0].sections[0];
    expect(section.objectives).toContain('理解正数和负数的意义');
    expect(section.concepts.join(' ')).toContain('定义');
    expect(section.formulas.join(' ')).toContain('a + (-a) = 0');
    expect(doc.blocks.some((block) => block.block_type === 'table')).toBe(true);
    expect(doc.blocks.some((block) => block.block_type === 'image')).toBe(true);
    const markdown = buildLessonMarkdown(section, '教材《数学》· 第 1-20 行');
    expect(markdown).toContain('试学版');
    expect(markdown).toContain('正数与负数的概念');
    expect(markdown).toContain('a + (-a) = 0');
    const second = doc.chapters[0].sections[1];
    const secondMarkdown = buildLessonMarkdown(second, '教材《数学》· 第 20-30 行');
    expect(secondMarkdown).toContain('| 运算 | 符号 |');
    expect(secondMarkdown).toContain('images/number-line.png');
  });

  it('没有明确目标或知识点时标记待确认，不编造内容', () => {
    const doc = parseStructuredDocument('这是一段没有标题和目标的教材说明文字，只描述了课程范围。');
    expect(doc.chapters).toHaveLength(1);
    const section = doc.chapters[0].sections[0];
    expect(section.objectives).toEqual([]);
    expect(section.knowledge_points).toHaveLength(1);
    expect(section.knowledge_points[0].status).toBe('needs_review');
    expect(section.status).toBe('needs_review');
  });

  it('PDF 文本中的页码标记会保留到原文块', () => {
    const doc = parseStructuredDocument('页码：2\n第一章 函数\n1.1 函数概念\n页码：3\n函数是描述两个变量关系的规则。');
    expect(doc.blocks.some((block) => block.page === 2)).toBe(true);
    expect(doc.blocks.some((block) => block.page === 3)).toBe(true);
  });

  it('空内容不会抛异常，也不会凭空生成章节', () => {
    const doc = parseStructuredDocument('   \n\n  ');
    expect(doc.blocks).toEqual([]);
    expect(doc.chapters).toEqual([]);
  });
});
